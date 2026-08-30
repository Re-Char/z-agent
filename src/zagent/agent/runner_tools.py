from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field

from zagent.domain.errors import ToolExecutionError
from zagent.security.permissions import PermissionBroker
from zagent.security.sandbox import SandboxLauncher, SandboxPolicy

from .fs_tools import is_protected_workspace_path


@dataclass(frozen=True)
class RunnerProfile:
    executable: str
    args: tuple[str, ...]
    description: str


def _npm_executable() -> str:
    return shutil.which("npm") or "npm"


RUNNER_PROFILES = {
    "python_unittest": RunnerProfile(
        sys.executable,
        ("-m", "unittest", "discover", "-s", "tests", "-v"),
        "在隔离快照中运行 Python unittest discover",
    ),
    "python_pytest": RunnerProfile(
        sys.executable,
        ("-m", "pytest", "-q", "--disable-warnings", "--maxfail=1"),
        "在隔离快照中运行 pytest（不接受任意参数）",
    ),
    "npm_test": RunnerProfile(
        _npm_executable(),
        ("test",),
        "在隔离快照中运行 package.json 已声明的 npm test",
    ),
}


class StrictRunnerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunnerExecuteArgs(StrictRunnerArgs):
    profile: str = Field(pattern=r"^(python_unittest|python_pytest|npm_test)$")
    timeout_seconds: int = Field(default=120, ge=1, le=300)


def runner_tool_schemas() -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": "runner_execute",
            "description": (
                "在去除密钥/依赖/版本库的只读项目快照中运行预批准测试模板。"
                "命令不可自定义，默认禁止网络，受超时和输出上限约束；每次执行需要权限批准。"
            ),
            "parameters": RunnerExecuteArgs.model_json_schema(),
        },
    }]


class ControlledRunnerExecutor:
    """Deny-by-default test runner with fixed templates and evidence-rich results."""

    def __init__(
        self,
        workspace_path_provider,
        permissions: PermissionBroker,
        *,
        launcher: SandboxLauncher | None = None,
        sandbox_enabled: bool = True,
        output_limit: int = 64 * 1024,
        snapshot_file_limit: int = 5000,
        snapshot_byte_limit: int = 100 * 1024 * 1024,
    ) -> None:
        self._workspace_path = workspace_path_provider
        self._permissions = permissions
        self._launcher = launcher or SandboxLauncher()
        self._sandbox_enabled = sandbox_enabled
        self._output_limit = output_limit
        self._snapshot_file_limit = snapshot_file_limit
        self._snapshot_byte_limit = snapshot_byte_limit

    @property
    def schemas(self) -> list[dict]:
        return runner_tool_schemas()

    def execute(self, session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name != "runner_execute":
            raise ToolExecutionError(f"unknown runner tool: {name}")
        try:
            args = RunnerExecuteArgs.model_validate(arguments)
        except Exception as exc:
            raise ToolExecutionError(f"invalid arguments for runner_execute: {exc}") from exc
        profile = RUNNER_PROFILES[args.profile]
        permission_arguments = {
            "profile": args.profile,
            "timeout_seconds": args.timeout_seconds,
            "network": False,
        }
        self._permissions.require(
            session_id,
            "runner",
            args.profile,
            "execute",
            permission_arguments,
            {"command_template": [profile.executable, *profile.args], "network": False},
        )
        return self._run(session_id, args.profile, profile, args.timeout_seconds)

    def _run(
        self, session_id: str, profile_name: str, profile: RunnerProfile, timeout: int
    ) -> Dict[str, Any]:
        workspace_value = self._workspace_path(session_id)
        if not workspace_value:
            raise ToolExecutionError("当前工作区未设置路径，Runner 已拒绝执行")
        workspace = Path(workspace_value).resolve()
        if not workspace.is_dir():
            raise ToolExecutionError("Runner 工作区不存在或不是目录")
        runner_root = workspace / ".zagent-runner"
        snapshot = runner_root / ("run_" + uuid.uuid4().hex)
        snapshot.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        try:
            snapshot_info = self._copy_snapshot(workspace, snapshot)
            read_paths = [str(snapshot)]
            node_modules = workspace / "node_modules"
            if profile_name == "npm_test" and node_modules.is_dir():
                read_paths.append(str(node_modules))
            policy = SandboxPolicy.build(read_paths, [str(snapshot)], network=False)
            try:
                command = self._launcher.wrap(
                    profile.executable,
                    list(profile.args),
                    policy,
                    enabled=self._sandbox_enabled,
                )
            except Exception as exc:
                raise ToolExecutionError(f"Runner OS 沙箱不可用，已拒绝执行：{exc}") from exc
            execution = self._execute_process(command, snapshot, timeout)
            elapsed = round(time.monotonic() - started, 3)
            return {
                "ok": execution["exit_code"] == 0 and not execution["timed_out"],
                "provenance": "controlled-runner",
                "profile": profile_name,
                "command_template": [profile.executable, *profile.args],
                "network": False,
                "sandboxed": self._sandbox_enabled,
                "snapshot_sha256": snapshot_info["sha256"],
                "snapshot_files": snapshot_info["files"],
                "exit_code": execution["exit_code"],
                "timed_out": execution["timed_out"],
                "output": execution["output"],
                "output_truncated": execution["output_truncated"],
                "duration_seconds": elapsed,
            }
        finally:
            shutil.rmtree(snapshot, ignore_errors=True)
            with contextlib.suppress(OSError):
                runner_root.rmdir()

    def _copy_snapshot(self, workspace: Path, snapshot: Path) -> Dict[str, Any]:
        digest = hashlib.sha256()
        count = 0
        total = 0
        for root, directories, files in os.walk(workspace, topdown=True, followlinks=False):
            root_path = Path(root)
            directories[:] = sorted(
                directory
                for directory in directories
                if not (root_path / directory).is_symlink()
                and not is_protected_workspace_path(
                    (root_path / directory).relative_to(workspace)
                )
            )
            for filename in sorted(files):
                source = root_path / filename
                relative = source.relative_to(workspace)
                if is_protected_workspace_path(relative) or source.is_symlink():
                    continue
                size = source.stat().st_size
                count += 1
                total += size
                if count > self._snapshot_file_limit or total > self._snapshot_byte_limit:
                    raise ToolExecutionError("Runner 项目快照超过文件数或总大小限制")
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                digest.update(str(relative).encode("utf-8"))
                digest.update(b"\0")
                with source.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        digest.update(chunk)
        return {"sha256": digest.hexdigest(), "files": count, "bytes": total}

    def _execute_process(self, command: list[str], cwd: Path, timeout: int) -> Dict[str, Any]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"}
        }
        environment.update({
            "TMPDIR": str(cwd / ".tmp"),
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "socks5://127.0.0.1:9",
            "NO_PROXY": "",
            "no_proxy": "",
            "PIP_NO_INDEX": "1",
            "npm_config_offline": "true",
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        (cwd / ".tmp").mkdir(exist_ok=True)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        output = bytearray()
        truncated = False

        def drain() -> None:
            nonlocal truncated
            assert process.stdout is not None
            while chunk := process.stdout.read(4096):
                remaining = self._output_limit - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)
        return {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output": output.decode("utf-8", errors="replace"),
            "output_truncated": truncated,
        }
