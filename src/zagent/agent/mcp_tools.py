from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Dict

from zagent.domain.errors import PermissionRequiredError, ToolExecutionError
from zagent.extensions import MCPManager
from zagent.security import PermissionBroker

_UNSAFE_TOOL_CHARACTER = re.compile(r"[^A-Za-z0-9_-]+")


class MCPToolExecutor:
    """Expose explicitly approved MCP tools to the provider's native tool-calling loop."""

    def __init__(self, manager: MCPManager, permissions: PermissionBroker) -> None:
        self._manager = manager
        self._permissions = permissions
        self._dispatch: Dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()

    @property
    def schemas(self) -> list[dict]:
        schemas: list[dict] = []
        dispatch: Dict[str, tuple[str, str]] = {}
        for server in self._manager.list_servers():
            if not server["enabled"] or not server["approved"] or server["transport"] != "stdio":
                continue
            try:
                tools = self._manager.list_tools(server["name"])
            except Exception:  # A broken optional server must not block the base agent runtime.
                continue
            for tool in tools:
                name = tool.get("name")
                schema = tool.get("inputSchema")
                if not isinstance(name, str) or not name or not isinstance(schema, dict):
                    continue
                alias = self._alias(server["name"], name)
                dispatch[alias] = (server["name"], name)
                description = str(tool.get("description") or f"MCP tool {name}")
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": alias,
                            "description": f"[MCP: {server['name']}] {description}"[:1024],
                            "parameters": schema,
                        },
                    }
                )
        with self._lock:
            self._dispatch = dispatch
        return schemas

    def execute(self, _session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            target = self._dispatch.get(name)
        if target is None:
            # Refresh once so servers approved after container startup become available.
            _ = self.schemas
            with self._lock:
                target = self._dispatch.get(name)
        if target is None:
            raise ToolExecutionError(f"unknown or unapproved MCP tool: {name}")
        server_name, tool_name = target
        try:
            self._permissions.require(
                _session_id,
                "mcp",
                server_name,
                f"tool:{tool_name}",
                arguments,
                {"server": server_name, "tool": tool_name},
            )
            result = self._manager.call_tool(server_name, tool_name, arguments)
        except PermissionRequiredError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"MCP tool {server_name}/{tool_name} failed: {exc}") from exc
        return {
            "ok": not bool(result.get("isError", False)),
            "mcp_server": server_name,
            "mcp_tool": tool_name,
            "result": result,
        }

    @staticmethod
    def _alias(server_name: str, tool_name: str) -> str:
        raw = f"mcp_{server_name}_{tool_name}"
        normalized = _UNSAFE_TOOL_CHARACTER.sub("_", raw).strip("_") or "mcp_tool"
        if len(normalized) <= 64:
            return normalized
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        return f"{normalized[:53]}_{suffix}"
