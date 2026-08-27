from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class MCPConfigRegistry:
    """Discovers MCP definitions only; execution requires later explicit approval."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "mcp.json"

    def list_servers(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        value = json.loads(self._path.read_text(encoding="utf-8"))
        servers = []
        for name, config in value.get("servers", {}).items():
            transport = config.get("transport", "stdio")
            servers.append({
                "name": name,
                "transport": transport,
                "enabled": bool(config.get("enabled", False)),
                "command": config.get("command") if transport == "stdio" else None,
                "url": config.get("url") if transport != "stdio" else None,
                "status": "configured",
            })
        return servers

