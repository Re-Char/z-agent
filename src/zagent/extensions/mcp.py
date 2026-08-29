from __future__ import annotations

import json
import re
import threading
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

from zagent.domain.errors import NotFoundError, ValidationError

from .mcp_client import MCPStdioClient
from .mcp_http import MCPStreamableHTTPClient
from .oauth import MCPOAuthManager

MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MCPConfigRegistry:
    """Persistent MCP server definitions. Secrets are referenced by env name, never stored."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "mcp.json"
        self._lock = threading.RLock()

    def list_servers(self) -> List[Dict[str, Any]]:
        with self._lock:
            servers = self._read().get("servers", {})
            return [self._server_to_dict(name, config) for name, config in sorted(servers.items())]

    def get_server(self, name: str) -> Dict[str, Any]:
        with self._lock:
            config = self._read().get("servers", {}).get(name)
            if not isinstance(config, dict):
                raise NotFoundError(f"MCP server not found: {name}")
            return self._server_to_dict(name, config)

    def add_server(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        name = str(spec.get("name", "")).strip()
        if not MCP_NAME_RE.fullmatch(name):
            raise ValidationError("invalid MCP server name")
        transport = str(spec.get("transport", "stdio"))
        if transport not in {"stdio", "http", "sse"}:
            raise ValidationError("transport must be one of: stdio, http, sse")
        if transport == "stdio":
            command = str(spec.get("command", "")).strip()
            if not command or "\x00" in command:
                raise ValidationError("stdio servers require a valid command")
            args = [str(item) for item in spec.get("args", [])]
            if len(args) > 128 or any(len(item) > 4_096 or "\x00" in item for item in args):
                raise ValidationError("MCP argument list exceeds safety limits")
            env = [str(item) for item in spec.get("env", [])]
            if len(env) > 64 or any(not ENV_NAME_RE.fullmatch(item) for item in env):
                raise ValidationError("MCP env must contain valid environment variable names")
            cwd_value = spec.get("cwd")
            cwd = str(cwd_value).strip() if cwd_value else None
            timeout_seconds = float(spec.get("timeout_seconds", 15.0))
            if not 0.1 <= timeout_seconds <= 300:
                raise ValidationError("MCP timeout_seconds must be between 0.1 and 300")
            config: Dict[str, Any] = {
                "transport": "stdio",
                "command": command,
                "args": args,
                "cwd": cwd,
                "env": env,
                "timeout_seconds": timeout_seconds,
                "sandbox": bool(spec.get("sandbox", True)),
                "sandbox_read_roots": [str(item) for item in spec.get("sandbox_read_roots", [])],
                "sandbox_write_roots": [str(item) for item in spec.get("sandbox_write_roots", [])],
                "network": bool(spec.get("network", False)),
            }
        else:
            url = str(spec.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                raise ValidationError("http/sse servers require an http(s) url")
            timeout_seconds = float(spec.get("timeout_seconds", 30.0))
            if not 0.1 <= timeout_seconds <= 300:
                raise ValidationError("MCP timeout_seconds must be between 0.1 and 300")
            config = {
                "transport": transport,
                "url": url,
                "timeout_seconds": timeout_seconds,
                "oauth": bool(spec.get("oauth", False)),
                "oauth_client_id": str(spec.get("oauth_client_id", "")),
                "oauth_scopes": [str(item) for item in spec.get("oauth_scopes", [])],
                "oauth_redirect_uri": str(spec.get("oauth_redirect_uri", "")),
                "registry": spec.get("registry") if isinstance(spec.get("registry"), dict) else None,
            }
        config["enabled"] = bool(spec.get("enabled", True))
        config["approved"] = bool(spec.get("approved", False))
        with self._lock:
            value = self._read()
            value.setdefault("servers", {})[name] = config
            self._write(value)
        return self._server_to_dict(name, config)

    def set_state(
        self, name: str, *, enabled: Optional[bool] = None, approved: Optional[bool] = None
    ) -> Dict[str, Any]:
        with self._lock:
            value = self._read()
            servers = value.get("servers", {})
            config = servers.get(name)
            if not isinstance(config, dict):
                raise NotFoundError(f"MCP server not found: {name}")
            if enabled is not None:
                config["enabled"] = bool(enabled)
            if approved is not None:
                config["approved"] = bool(approved)
            self._write(value)
            return self._server_to_dict(name, config)

    def remove_server(self, name: str) -> bool:
        with self._lock:
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
        enabled = bool(config.get("enabled", False))
        approved = bool(config.get("approved", False))
        if not enabled:
            status = "disabled"
        elif not approved:
            status = "approval_required"
        elif transport == "sse":
            status = "legacy_transport_unsupported"
        else:
            status = "ready"
        return {
            "name": name,
            "transport": transport,
            "enabled": enabled,
            "approved": approved,
            "command": config.get("command") if transport == "stdio" else None,
            "args": list(config.get("args", [])) if transport == "stdio" else None,
            "cwd": config.get("cwd") if transport == "stdio" else None,
            "env": list(config.get("env", [])) if transport == "stdio" else None,
            "timeout_seconds": config.get("timeout_seconds", 15.0),
            "sandbox": bool(config.get("sandbox", True)) if transport == "stdio" else None,
            "sandbox_read_roots": list(config.get("sandbox_read_roots", []))
            if transport == "stdio"
            else None,
            "sandbox_write_roots": list(config.get("sandbox_write_roots", []))
            if transport == "stdio"
            else None,
            "network": bool(config.get("network", False)) if transport == "stdio" else True,
            "url": config.get("url") if transport != "stdio" else None,
            "oauth": bool(config.get("oauth", False)) if transport != "stdio" else False,
            "oauth_client_id": config.get("oauth_client_id", "") if transport != "stdio" else "",
            "oauth_scopes": list(config.get("oauth_scopes", [])) if transport != "stdio" else [],
            "oauth_redirect_uri": config.get("oauth_redirect_uri", "")
            if transport != "stdio"
            else "",
            "registry": config.get("registry") if transport != "stdio" else None,
            "status": status,
        }

    def _read(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": 1, "servers": {}}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid MCP configuration: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("servers", {}), dict):
            raise ValidationError("invalid MCP configuration structure")
        return value

    def _write(self, value: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        value["schema_version"] = 1
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)


class MCPManager:
    """Owns MCP client processes and requires explicit approval before execution."""

    def __init__(
        self, registry: MCPConfigRegistry, oauth: Optional[MCPOAuthManager] = None
    ) -> None:
        self.registry = registry
        self.oauth = oauth
        self._clients: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def list_servers(self) -> List[Dict[str, Any]]:
        servers = self.registry.list_servers()
        with self._lock:
            for server in servers:
                client = self._clients.get(server["name"])
                if client and client.connected:
                    server["status"] = "connected"
                    server["protocol_version"] = client.protocol_version
                    server["server_info"] = client.server_info
        return servers

    def add_server(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        name = str(spec.get("name", "")).strip()
        server = self.registry.add_server(spec)
        self.disconnect(name)
        return server

    def set_state(
        self, name: str, *, enabled: Optional[bool] = None, approved: Optional[bool] = None
    ) -> Dict[str, Any]:
        server = self.registry.set_state(name, enabled=enabled, approved=approved)
        if not server["enabled"] or not server["approved"]:
            self.disconnect(name)
        return server

    def remove_server(self, name: str) -> bool:
        self.disconnect(name)
        return self.registry.remove_server(name)

    def connect(self, name: str) -> Dict[str, Any]:
        server = self.registry.get_server(name)
        self._require_execution_allowed(server)
        if server["transport"] == "sse":
            raise ValidationError("legacy MCP SSE transport is not supported; use Streamable HTTP")
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                if server["transport"] == "stdio":
                    client = MCPStdioClient(
                        server["command"],
                        server["args"] or [],
                        cwd=server.get("cwd"),
                        env_names=server.get("env") or [],
                        timeout_seconds=float(server.get("timeout_seconds") or 15.0),
                        sandbox=bool(server.get("sandbox", True)),
                        sandbox_read_roots=server.get("sandbox_read_roots") or [],
                        sandbox_write_roots=server.get("sandbox_write_roots") or [],
                        network=bool(server.get("network", False)),
                    )
                else:
                    if server.get("oauth") and self.oauth is None:
                        raise ValidationError("MCP OAuth manager is unavailable")
                    token_provider = (
                        partial(self.oauth.access_token, name)
                        if server.get("oauth") and self.oauth
                        else None
                    )
                    client = MCPStreamableHTTPClient(
                        server["url"],
                        timeout_seconds=float(server.get("timeout_seconds") or 30.0),
                        token_provider=token_provider,
                    )
                self._clients[name] = client
            try:
                info = client.connect()
            except Exception:
                self._clients.pop(name, None)
                raise
        return {"name": name, **info}

    def list_tools(self, name: str) -> List[Dict[str, Any]]:
        client = self._connected_client(name)
        return client.list_tools()

    def call_tool(self, name: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        client = self._connected_client(name)
        return client.call_tool(tool_name, arguments)

    def disconnect(self, name: str) -> bool:
        with self._lock:
            client = self._clients.pop(name, None)
        if client is None:
            return False
        client.close()
        return True

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def _connected_client(self, name: str) -> Any:
        server = self.registry.get_server(name)
        self._require_execution_allowed(server)
        with self._lock:
            client = self._clients.get(name)
        if client is None or not client.connected:
            self.connect(name)
            with self._lock:
                client = self._clients[name]
        return client

    @staticmethod
    def _require_execution_allowed(server: Dict[str, Any]) -> None:
        if not server["enabled"]:
            raise ValidationError("MCP server is disabled")
        if not server["approved"]:
            raise ValidationError("MCP server execution requires explicit approval")
