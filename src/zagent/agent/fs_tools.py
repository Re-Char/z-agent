from __future__ import annotations

import fnmatch
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from zagent.domain.errors import ToolExecutionError

FS_TOOL_DESCRIPTIONS = {
    "fs_list": "列出当前工作区某个目录下的条目（文件名、类型、大小、修改时间），默认项目根目录，不递归",
    "fs_mkdir": "在当前工作区内创建目录，可递归创建父目录；不能访问工作区外、敏感或受保护目录",
    "fs_read": "读取当前工作区内单个非敏感文本文件的最新内容，并返回 sha256 版本指纹。"
               "max_chars 按字符数截断（中文 1 字符 ≈ 1 token）",
    "fs_search": "在当前工作区内按关键词搜索文件名与文件内容（大小写不敏感）。"
                 "文件大于 512KB 时只扫描头部 32K 字符；此类内容命中返回 partial=true",
    "fs_project_overview": "生成项目顶层结构概览：目录树（2 层）+ 主要文件清单，用于快速了解项目",
    "fs_write": "在当前工作区创建或完整更新非敏感文本文件。更新已有文件前必须先调用 fs_read，"
                "并传回 expected_sha256，防止覆盖比模型所读版本更新的代码。不支持删除或执行文件",
    "fs_replace": "在当前工作区非敏感文本文件中做精确字符串替换。必须传入 fs_read 返回的"
                  "expected_sha256；默认要求 old_text 只出现一次，避免误改",
}


class StrictFsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FsListArgs(StrictFsArgs):
    path: str = Field(default=".", max_length=2000)


class FsMkdirArgs(StrictFsArgs):
    path: str = Field(min_length=1, max_length=2000)


class FsReadArgs(StrictFsArgs):
    path: str = Field(min_length=1, max_length=2000)
    max_chars: int = Field(default=20_000, ge=256, le=100_000)


class FsSearchArgs(StrictFsArgs):
    query: str = Field(min_length=1, max_length=200)
    path: str = Field(default=".", max_length=2000)
    limit: int = Field(default=20, ge=1, le=50)


class FsOverviewArgs(StrictFsArgs):
    pass


class FsWriteArgs(StrictFsArgs):
    path: str = Field(min_length=1, max_length=2000)
    content: str = Field(max_length=200_000)
    expected_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FsReplaceArgs(StrictFsArgs):
    path: str = Field(min_length=1, max_length=2000)
    old_text: str = Field(min_length=1, max_length=100_000)
    new_text: str = Field(max_length=100_000)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replace_all: bool = False


FS_ARGUMENT_TYPES = {
    "fs_list": FsListArgs,
    "fs_mkdir": FsMkdirArgs,
    "fs_read": FsReadArgs,
    "fs_search": FsSearchArgs,
    "fs_project_overview": FsOverviewArgs,
    "fs_write": FsWriteArgs,
    "fs_replace": FsReplaceArgs,
}

# 跳过不参与搜索/列出的目录与文件（vcs、依赖、构建产物、缓存）
_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".conda", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build", "target", ".next",
    ".turbo", "coverage", ".coverage", ".idea", ".vscode", ".DS_Store",
    ".zagent-runner",
}
_IGNORED_FILES = {".DS_Store", "*.pyc", "*.pyo", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}

_SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".azure", ".gcloud"}
_SENSITIVE_NAMES = {
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".envrc", "credentials",
    "credentials.json", "credentials.tfrc.json", "terraform.tfvars", "kubeconfig",
    "service-account.json", "service_account.json", "secrets.json", "secrets.yaml",
    "secrets.yml", "secrets.toml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}


def is_protected_workspace_path(relative: Path) -> bool:
    """Shared deny-list for Agent file tools and controlled runner snapshots."""
    parts = [part.casefold() for part in relative.parts]
    if any(part in _SENSITIVE_DIRS or part in _IGNORED_DIRS for part in parts):
        return True
    if not parts:
        return False
    name = parts[-1]
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.startswith(".envrc.")
        or name in _SENSITIVE_NAMES
        or name.endswith(".tfvars")
        or Path(name).suffix.casefold() in _SENSITIVE_SUFFIXES
    )


def fs_tool_schemas() -> List[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": FS_TOOL_DESCRIPTIONS[name],
                "parameters": argument_type.model_json_schema(),
            },
        }
        for name, argument_type in FS_ARGUMENT_TYPES.items()
    ]


class FileSystemToolExecutor:
    """Version-checked text tools confined to the active workspace directory.

    This is the security boundary of the agent: every path is resolved and must
    stay inside the workspace root (symlinks included); anything else is refused.
    """

    def __init__(self, workspace_path_provider) -> None:
        self._workspace_path = workspace_path_provider

    @property
    def schemas(self) -> List[dict]:
        return fs_tool_schemas()

    def execute(self, session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        argument_type = FS_ARGUMENT_TYPES.get(name)
        if argument_type is None:
            raise ToolExecutionError(f"unknown filesystem tool: {name}")
        try:
            args = argument_type.model_validate(arguments)
        except Exception as exc:  # pydantic ValidationError
            raise ToolExecutionError(f"invalid arguments for {name}: {exc}") from exc
        if name == "fs_list":
            return self._list(session_id, args.path)
        if name == "fs_mkdir":
            return self._mkdir(session_id, args.path)
        if name == "fs_read":
            return self._read(session_id, args.path, args.max_chars)
        if name == "fs_search":
            return self._search(session_id, args.query, args.path, args.limit)
        if name == "fs_project_overview":
            return self._overview(session_id)
        if name == "fs_write":
            return self._write(session_id, args.path, args.content, args.expected_sha256)
        if name == "fs_replace":
            return self._replace(
                session_id, args.path, args.old_text, args.new_text,
                args.expected_sha256, args.replace_all,
            )
        raise ToolExecutionError(f"unhandled filesystem tool: {name}")

    # --- path resolution ----------------------------------------------------

    def _resolve(self, session_id: str, relative: str) -> Path:
        root = self._workspace_path(session_id)
        if not root:
            raise ToolExecutionError(
                "当前工作区未设置路径：请在工作区设置中指定 agent 可访问的项目目录"
            )
        root_path = Path(root).resolve()
        target = (root_path / relative).resolve()
        if target != root_path and root_path not in target.parents:
            raise ToolExecutionError(f"路径超出工作区边界，已拒绝：{relative}")
        if self._is_sensitive(target.relative_to(root_path)):
            raise ToolExecutionError(f"敏感文件受保护，禁止读取或修改：{relative}")
        return target

    @staticmethod
    def _is_sensitive(relative: Path) -> bool:
        return is_protected_workspace_path(relative)

    @staticmethod
    def _is_binary(target: Path) -> bool:
        try:
            with target.open("rb") as stream:
                return b"\x00" in stream.read(8192)
        except OSError:
            return True

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _workspace_exists(self, session_id: str) -> bool:
        root = self._workspace_path(session_id)
        return bool(root) and Path(root).is_dir()

    # --- tools ---------------------------------------------------------------

    def _list(self, session_id: str, relative: str) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        root = Path(self._workspace_path(session_id)).resolve()
        if not target.exists():
            raise ToolExecutionError(f"目录不存在：{relative}")
        if not target.is_dir():
            raise ToolExecutionError(f"不是目录：{relative}")
        entries: List[Dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.name in _IGNORED_DIRS or child.name in _IGNORED_FILES:
                continue
            try:
                resolved_relative = child.resolve().relative_to(root)
            except ValueError:
                continue
            if self._is_sensitive(resolved_relative):
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
                "modified": int(stat.st_mtime),
            })
        return {"path": str(target), "entries": entries[:200], "count": len(entries)}

    def _mkdir(self, session_id: str, relative: str) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        root = Path(self._workspace_path(session_id)).resolve()
        if target == root:
            return {"path": relative, "created": False}
        if target.exists():
            if not target.is_dir():
                raise ToolExecutionError(f"同名文件已存在，无法创建目录：{relative}")
            return {"path": relative, "created": False}
        try:
            target.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise ToolExecutionError(f"创建目录失败：{exc}") from exc
        return {"path": relative, "created": True}

    def _read(self, session_id: str, relative: str, max_chars: int) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        if not target.is_file():
            raise ToolExecutionError(f"文件不存在：{relative}")
        if target.stat().st_size > 2_000_000:
            raise ToolExecutionError(f"文件过大（>{2_000_000} 字节），请改用 fs_search 定位相关片段")
        if self._is_binary(target):
            raise ToolExecutionError(f"二进制文件不允许读取：{relative}")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolExecutionError(f"读取失败：{exc}") from exc
        truncated = len(text) > max_chars
        return {
            "path": relative,
            "chars": len(text),
            "truncated": truncated,
            "content": text[:max_chars],
            "sha256": self._sha256(text),
            "modified_ns": target.stat().st_mtime_ns,
        }

    def _search(self, session_id: str, query: str, relative: str, limit: int) -> Dict[str, Any]:
        root = self._resolve(session_id, ".")
        target = self._resolve(session_id, relative)
        if not target.is_dir():
            raise ToolExecutionError(f"不是目录：{relative}")
        needle = query.casefold()
        hits: List[Dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
            for filename in sorted(filenames):
                if any(fnmatch.fnmatch(filename, pattern) for pattern in _IGNORED_FILES):
                    continue
                full = Path(dirpath) / filename
                try:
                    rel = full.relative_to(root)
                    resolved_rel = full.resolve().relative_to(root)
                except ValueError:
                    continue
                if self._is_sensitive(rel) or self._is_sensitive(resolved_rel) or self._is_binary(full):
                    continue
                if needle in filename.casefold():
                    hits.append({"path": str(rel), "kind": "filename", "partial": False})
                else:
                    try:
                        size = full.stat().st_size
                        partial = size > 512_000
                        # Read only the inspected prefix instead of loading a potentially
                        # huge file into memory.  `partial` makes the incomplete scan explicit.
                        with full.open("r", encoding="utf-8", errors="replace") as stream:
                            head = stream.read(32_000 if partial else 512_001)
                    except OSError:
                        continue
                    if needle in head.casefold():
                        hits.append({"path": str(rel), "kind": "content", "partial": partial})
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
        return {"query": query, "hits": hits[:limit], "count": len(hits)}

    def _overview(self, session_id: str) -> Dict[str, Any]:
        root = self._resolve(session_id, ".")
        if not self._workspace_exists(session_id):
            raise ToolExecutionError("当前工作区未设置路径")
        tree: List[Dict[str, Any]] = []
        top_items = (
            item for item in root.iterdir()
            if item.name not in _IGNORED_DIRS
            and not self._is_sensitive(item.relative_to(root))
        )
        top = sorted(top_items, key=lambda item: item.name.lower())
        for item in top:
            if item.is_dir():
                children = sorted(
                    (
                        c for c in item.iterdir()
                        if c.name not in _IGNORED_DIRS
                        and not c.name.startswith(".")
                        and not self._is_sensitive(c.relative_to(root))
                    ),
                    key=lambda c: c.name.lower(),
                )[:12]
                tree.append({"type": "dir", "name": item.name, "children": [c.name for c in children]})
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                tree.append({"type": "file", "name": item.name, "size": size})
        return {"root": str(root), "top_level": tree[:60]}

    def _write(
        self, session_id: str, relative: str, content: str, expected_sha256: Optional[str]
    ) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        if not target.parent.is_dir():
            raise ToolExecutionError(f"父目录不存在：{target.parent}")
        existed = target.exists()
        previous_sha256 = None
        mode = 0o644
        if existed:
            if not target.is_file() or self._is_binary(target):
                raise ToolExecutionError(f"只能修改普通文本文件：{relative}")
            if target.stat().st_size > 2_000_000:
                raise ToolExecutionError("文件过大，拒绝完整覆盖")
            current = target.read_text(encoding="utf-8", errors="replace")
            previous_sha256 = self._sha256(current)
            if expected_sha256 is None:
                raise ToolExecutionError("更新已有文件前必须先 fs_read，并传入 expected_sha256")
            if expected_sha256 != previous_sha256:
                raise ToolExecutionError("文件已在读取后发生变化，请重新 fs_read 后再修改")
            mode = target.stat().st_mode & 0o777
        elif expected_sha256 is not None:
            raise ToolExecutionError("目标文件不存在；创建新文件时不要传 expected_sha256")

        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.zagent-",
                delete=False,
            ) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                temp_name = stream.name
            os.chmod(temp_name, mode)
            os.replace(temp_name, target)
            temp_name = None
        except OSError as exc:
            raise ToolExecutionError(f"写入失败：{exc}") from exc
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
        return {
            "path": relative,
            "created": not existed,
            "chars": len(content),
            "previous_sha256": previous_sha256,
            "sha256": self._sha256(content),
        }

    def _replace(
        self,
        session_id: str,
        relative: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        replace_all: bool,
    ) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        if not target.is_file() or self._is_binary(target):
            raise ToolExecutionError(f"只能修改普通文本文件：{relative}")
        current = target.read_text(encoding="utf-8", errors="replace")
        current_sha256 = self._sha256(current)
        if current_sha256 != expected_sha256:
            raise ToolExecutionError("文件已在读取后发生变化，请重新 fs_read 后再修改")
        count = current.count(old_text)
        if count == 0:
            raise ToolExecutionError("未找到要替换的原文；请重新读取最新代码")
        if count > 1 and not replace_all:
            raise ToolExecutionError(f"原文出现 {count} 次；请提供更精确片段或显式设置 replace_all")
        updated = (
            current.replace(old_text, new_text)
            if replace_all
            else current.replace(old_text, new_text, 1)
        )
        result = self._write(session_id, relative, updated, current_sha256)
        result["replacements"] = count if replace_all else 1
        return result
