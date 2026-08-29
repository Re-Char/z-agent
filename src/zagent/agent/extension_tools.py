from __future__ import annotations

import threading
from typing import Any, Dict

from zagent.domain.errors import PermissionRequiredError, ToolExecutionError
from zagent.extensions.host import ExtensionHostManager, extension_tool_alias
from zagent.extensions.manifest import ExtensionRegistry


class ExtensionToolExecutor:
    def __init__(self, registry: ExtensionRegistry, hosts: ExtensionHostManager) -> None:
        self._registry = registry
        self._hosts = hosts
        self._dispatch: Dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()

    @property
    def schemas(self) -> list[dict]:
        schemas: list[dict] = []
        dispatch: Dict[str, tuple[str, str]] = {}
        for extension in self._registry.discover():
            if not extension.enabled or extension.runtime not in {"python", "node"}:
                continue
            try:
                tools = self._hosts.list_tools(extension.extension_id)
            except Exception:
                continue
            for tool in tools:
                name = tool.get("name")
                parameters = tool.get("inputSchema")
                if not isinstance(name, str) or not isinstance(parameters, dict):
                    continue
                alias = extension_tool_alias(extension.extension_id, name)
                dispatch[alias] = (extension.extension_id, name)
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": alias,
                            "description": f"[Extension: {extension.extension_id}] "
                            f"{tool.get('description') or name}"[:1024],
                            "parameters": parameters,
                        },
                    }
                )
        with self._lock:
            self._dispatch = dispatch
        return schemas

    def execute(self, session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            target = self._dispatch.get(name)
        if target is None:
            _ = self.schemas
            with self._lock:
                target = self._dispatch.get(name)
        if target is None:
            raise ToolExecutionError(f"unknown or unavailable extension tool: {name}")
        extension_id, tool_name = target
        try:
            result = self._hosts.call_tool(extension_id, tool_name, arguments, session_id)
        except PermissionRequiredError:
            raise
        except Exception as exc:
            raise ToolExecutionError(
                f"extension tool {extension_id}/{tool_name} failed: {exc}"
            ) from exc
        return {
            "ok": not bool(result.get("isError", False)),
            "extension_id": extension_id,
            "extension_tool": tool_name,
            "result": result,
        }
