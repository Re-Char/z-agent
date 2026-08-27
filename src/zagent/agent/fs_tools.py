from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from zagent.domain.errors import ToolExecutionError

FS_TOOL_DESCRIPTIONS = {
    "fs_list": "列出当前工作区某个目录下的条目（文件名、类型、大小、修改时间），默认项目根目录，不递归",
    "fs_read": "读取当前工作区内单个文件的文本内容。max_chars 按字符数截断"
               "（中文 1 字符 ≈ 1 token）；默认最多 20000 字符，单次最多 100000 字符",
    "fs_search": "在当前工作区内按关键词搜索文件名与文件内容（大小写不敏感）。"
                 "返回 partial=true 表示命中在未扫描的尾部（文件大于 512KB 时只扫头部 32KB）",
    "fs_project_overview": "生成项目顶层结构概览：目录树（2 层）+ 主要文件清单，用于快速了解项目",
}


class StrictFsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FsListArgs(StrictFsArgs):
    path: str = Field(default=".", max_length=2000)


class FsReadArgs(StrictFsArgs):
    path: str = Field(min_length=1, max_length=2000)
    max_chars: int = Field(default=20_000, ge=256, le=100_000)


class FsSearchArgs(StrictFsArgs):
    query: str = Field(min_length=1, max_length=200)
    path: str = Field(default=".", max_length=2000)
    limit: int = Field(default=20, ge=1, le=50)


class FsOverviewArgs(StrictFsArgs):
    pass


FS_ARGUMENT_TYPES = {
    "fs_list": FsListArgs,
    "fs_read": FsReadArgs,
    "fs_search": FsSearchArgs,
    "fs_project_overview": FsOverviewArgs,
}

# 跳过不参与搜索/列出的目录与文件（vcs、依赖、构建产物、缓存）
_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".conda", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build", "target", ".next",
    ".turbo", "coverage", ".coverage", ".idea", ".vscode", ".DS_Store",
}
_IGNORED_FILES = {".DS_Store", "*.pyc", "*.pyo", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}


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
    """Read-only filesystem tools confined to the active workspace directory.

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
        if name == "fs_read":
            return self._read(session_id, args.path, args.max_chars)
        if name == "fs_search":
            return self._search(session_id, args.query, args.path, args.limit)
        if name == "fs_project_overview":
            return self._overview(session_id)
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
        return target

    def _workspace_exists(self, session_id: str) -> bool:
        root = self._workspace_path(session_id)
        return bool(root) and Path(root).is_dir()

    # --- tools ---------------------------------------------------------------

    def _list(self, session_id: str, relative: str) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        if not target.exists():
            raise ToolExecutionError(f"目录不存在：{relative}")
        if not target.is_dir():
            raise ToolExecutionError(f"不是目录：{relative}")
        entries: List[Dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.name in _IGNORED_DIRS or child.name in _IGNORED_FILES:
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

    def _read(self, session_id: str, relative: str, max_chars: int) -> Dict[str, Any]:
        target = self._resolve(session_id, relative)
        if not target.is_file():
            raise ToolExecutionError(f"文件不存在：{relative}")
        if target.stat().st_size > 2_000_000:
            raise ToolExecutionError(f"文件过大（>{2_000_000} 字节），请改用 fs_search 定位相关片段")
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
                except ValueError:
                    continue
                if needle in filename.casefold():
                    hits.append({"path": str(rel), "kind": "filename", "partial": False})
                else:
                    try:
                        size = full.stat().st_size
                        if size > 512_000:
                            # Performance trade-off: only the head is scanned.
                            continue
                        head = full.read_text(encoding="utf-8", errors="replace")[:32_000]
                    except OSError:
                        continue
                    if needle in head.casefold():
                        hits.append({"path": str(rel), "kind": "content", "partial": False})
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
        top_items = (item for item in root.iterdir() if item.name not in _IGNORED_DIRS)
        top = sorted(top_items, key=lambda item: item.name.lower())
        for item in top:
            if item.is_dir():
                children = sorted(
                    (c for c in item.iterdir() if c.name not in _IGNORED_DIRS and not c.name.startswith(".")),
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
