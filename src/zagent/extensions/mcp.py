from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from zagent.domain.errors import ValidationError

MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class MCPConfigRegistry:
    """Discovers and manages MCP definitions; execution requires later explicit approval."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "mcp.json"

    def list_servers(self) -> List[Dict[str, Any]]:
        servers = []
        for name, config in self._read().get("servers", {}).items():
            transport = config.get("transport", "stdio")
            servers.append({
                "name": name,
                "transport": transport,
                "enabled": bool(config.get("enabled", False)),
                "command": config.get("command") if transport == "stdio" else None,
                "args": config.get("args", []) if transport == "stdio" else None,
                "url": config.get("url") if transport != "stdio" else None,
                "status": "configured",
            })
        return servers

    def add_server(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        name = str(spec.get("name", "")).strip()
        if not MCP_NAME_RE.fullmatch(name):
            raise ValidationError("invalid MCP server name")
        transport = str(spec.get("transport", "stdio"))
        if transport not in {"stdio", "http", "sse"}:
            raise ValidationError("transport must be one of: stdio, http, sse")
        if transport == "stdio":
            command = str(spec.get("command", "")).strip()
            if not command:
                raise ValidationError("stdio servers require a command")
            config: Dict[str, Any] = {
                "transport": "stdio",
                "command": command,
                "args": [str(item) for item in spec.get("args", [])],
            }
        else:
            url = str(spec.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                raise ValidationError("http/sse servers require an http(s) url")
            config = {"transport": transport, "url": url}
        config["enabled"] = bool(spec.get("enabled", True))
        value = self._read()
        value.setdefault("servers", {})[name] = config
        self._write(value)
        return self._server_to_dict(name, config)

    def remove_server(self, name: str) -> bool:
        value = self._read()
        servers = value.get("servers", {})
        if name not in servers:
            return False
        del servers[name]
        self._write(value)
        return True

    @staticmethod
    def _server_to_dict(name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        transport = config.get("transport", "stdio")
        return {
            "name": name,
            "transport": transport,
            "enabled": bool(config.get("enabled", False)),
            "command": config.get("command") if transport == "stdio" else None,
            "args": config.get("args", []) if transport == "stdio" else None,
            "url": config.get("url") if transport != "stdio" else None,
            "status": "configured",
        }

    def _read(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, value: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
