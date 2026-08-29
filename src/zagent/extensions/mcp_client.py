from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

from zagent import __version__
from zagent.domain.errors import ValidationError
from zagent.security.sandbox import SandboxLauncher, SandboxPolicy

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_STDERR_LINES = 100
BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


class MCPProtocolError(ValidationError):
    pass


class MCPStdioClient:
    """Small MCP JSON-RPC client with no agent framework or MCP SDK dependency."""

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        cwd: Optional[str] = None,
        env_names: Optional[list[str]] = None,
        timeout_seconds: float = 15.0,
        sandbox: bool = True,
        sandbox_read_roots: Optional[list[str]] = None,
        sandbox_write_roots: Optional[list[str]] = None,
        network: bool = False,
    ) -> None:
        self._command = command
        self._args = args
        self._cwd = cwd
        self._env_names = env_names or []
        self._timeout = timeout_seconds
        self._sandbox = sandbox
        self._sandbox_read_roots = sandbox_read_roots or []
        self._sandbox_write_roots = sandbox_write_roots or []
        self._network = network
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._pending: Dict[int, queue.Queue[Dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._fatal_error: Optional[str] = None
        self._stderr: deque[str] = deque(maxlen=MAX_STDERR_LINES)
        self.server_info: Dict[str, Any] = {}
        self.server_capabilities: Dict[str, Any] = {}
        self.protocol_version: Optional[str] = None

    @property
    def connected(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self.protocol_version is not None
        )

    @property
    def diagnostics(self) -> list[str]:
        return list(self._stderr)

    def connect(self) -> Dict[str, Any]:
        if self.connected:
            return self.connection_info()
        self._start()
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "z-agent", "version": __version__},
                },
            )
            version = str(result.get("protocolVersion", ""))
            if version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise MCPProtocolError(f"MCP server negotiated unsupported version: {version or 'missing'}")
            self.protocol_version = version
            self.server_info = self._object(result.get("serverInfo"), "serverInfo")
            self.server_capabilities = self._object(result.get("capabilities"), "capabilities")
            self.notify("notifications/initialized")
            return self.connection_info()
        except Exception:
            self.close()
            raise

    def connection_info(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "protocol_version": self.protocol_version,
            "server_info": self.server_info,
            "capabilities": self.server_capabilities,
            "diagnostics": self.diagnostics,
        }

    def list_tools(self) -> list[Dict[str, Any]]:
        self._require_connected()
        if "tools" not in self.server_capabilities:
            raise MCPProtocolError("MCP server did not advertise tools capability")
        tools: list[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors = set()
        while True:
            params = {"cursor": cursor} if cursor else None
            result = self.request("tools/list", params)
            page = result.get("tools", [])
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise MCPProtocolError("MCP tools/list returned an invalid tools array")
            tools.extend(page)
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return tools
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise MCPProtocolError("MCP tools/list returned an invalid pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._require_connected()
        if not name or len(name) > 128:
            raise ValidationError("invalid MCP tool name")
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPProtocolError("MCP tools/call returned a non-object result")
        return result

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        process = self._require_process()
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message, process.stdin)
            try:
                response = response_queue.get(timeout=self._timeout)
            except queue.Empty as exc:
                with suppress(MCPProtocolError):
                    self.notify(
                        "notifications/cancelled", {"requestId": request_id, "reason": "timeout"}
                    )
                diagnostics = self._diagnostic_suffix()
                raise MCPProtocolError(f"MCP request timed out: {method}{diagnostics}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            error = response.get("error")
            message_text = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPProtocolError(f"MCP {method} failed: {message_text}")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise MCPProtocolError(f"MCP {method} returned an invalid result")
        return result

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        process = self._require_process()
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message, process.stdin)

    def close(self) -> None:
        process = self._process
        self.protocol_version = None
        self.server_info = {}
        self.server_capabilities = {}
        if process is None:
            return
        self._process = None
        if process.stdin:
            with suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        self._fail_pending("MCP server connection closed")

    def _start(self) -> None:
        executable = self._resolve_command()
        cwd = None
        if self._cwd:
            cwd_path = Path(self._cwd).expanduser().resolve()
            if not cwd_path.is_dir():
                raise ValidationError(f"MCP working directory does not exist: {self._cwd}")
            cwd = str(cwd_path)
        env = {key: value for key in BASE_ENV_KEYS if (value := os.environ.get(key)) is not None}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in self._env_names:
            if not name or "=" in name or "\x00" in name:
                raise ValidationError(f"invalid MCP environment variable name: {name!r}")
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        try:
            automatic_read_roots = list(self._sandbox_read_roots)
            if cwd:
                automatic_read_roots.append(cwd)
            if self._args:
                first_argument = Path(self._args[0]).expanduser()
                if first_argument.exists():
                    automatic_read_roots.append(str(first_argument.resolve()))
            policy = SandboxPolicy.build(
                automatic_read_roots,
                self._sandbox_write_roots,
                network=self._network,
            )
            command = SandboxLauncher().wrap(
                executable, self._args, policy, enabled=self._sandbox
            )
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                shell=False,
            )
        except OSError as exc:
            raise ValidationError(f"failed to start MCP server: {exc}") from exc
        self._process = process
        self._fatal_error = None
        self._reader = threading.Thread(target=self._read_stdout, args=(process,), daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, args=(process,), daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _resolve_command(self) -> str:
        if "\x00" in self._command:
            raise ValidationError("invalid MCP command")
        command_path = Path(self._command).expanduser()
        if command_path.is_absolute():
            if not command_path.is_file():
                raise ValidationError(f"MCP executable does not exist: {command_path}")
            return str(command_path.resolve())
        resolved = shutil.which(self._command)
        if not resolved:
            raise ValidationError(f"MCP executable was not found on PATH: {self._command}")
        return resolved

    def _read_stdout(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        while self._process is process:
            line = process.stdout.readline(MAX_FRAME_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                self._fatal_error = "MCP server emitted an oversized or unterminated frame"
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._fatal_error = "MCP server wrote non-JSON data to stdout"
                break
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                self._fatal_error = "MCP server emitted an invalid JSON-RPC message"
                break
            if "id" in message and ("result" in message or "error" in message):
                with self._pending_lock:
                    target = self._pending.get(message["id"])
                if target:
                    with suppress(queue.Full):
                        target.put_nowait(message)
            elif "id" in message and "method" in message:
                self._handle_server_request(message, process.stdin)
        if self._process is process:
            reason = self._fatal_error or f"MCP server exited with code {process.poll()}"
            self._fail_pending(reason)

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        while self._process is process:
            line = process.stderr.readline(8_193)
            if not line:
                return
            self._stderr.append(line[:8_192].decode("utf-8", errors="replace").rstrip())

    def _handle_server_request(self, message: Dict[str, Any], stream: Optional[BinaryIO]) -> None:
        if message.get("method") == "ping":
            response = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Client method not supported"},
            }
        self._send(response, stream)

    def _send(self, message: Dict[str, Any], stream: Optional[BinaryIO]) -> None:
        if stream is None:
            raise MCPProtocolError("MCP server stdin is unavailable")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            try:
                stream.write(encoded)
                stream.flush()
            except (BrokenPipeError, OSError) as exc:
                raise MCPProtocolError(f"failed to write to MCP server: {exc}") from exc

    def _require_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None:
            diagnostics = self._diagnostic_suffix()
            raise MCPProtocolError(f"MCP server is not running{diagnostics}")
        if self._fatal_error:
            raise MCPProtocolError(self._fatal_error)
        return process

    def _require_connected(self) -> None:
        if not self.connected:
            raise MCPProtocolError("MCP client is not initialized")

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        error = {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": reason}}
        for target in pending:
            with suppress(queue.Full):
                target.put_nowait(error)

    def _diagnostic_suffix(self) -> str:
        return f" ({self._stderr[-1]})" if self._stderr else ""

    @staticmethod
    def _object(value: Any, field: str) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise MCPProtocolError(f"MCP initialize returned invalid {field}")
        return value
