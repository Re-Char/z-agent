from __future__ import annotations

import json
import re
import shutil
import stat
import sys
import tempfile
import threading
import zipfile
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from zagent.domain.errors import NotFoundError, ValidationError

from .mcp_client import MCPStdioClient
from .mcp_http import MCPStreamableHTTPClient
from .oauth import MCPOAuthManager

MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_IMPORT_BYTES = 1024 * 1024
MAX_BUNDLE_FILES = 16_384
MAX_BUNDLE_BYTES = 128 * 1024 * 1024


class MCPConfigRegistry:
    """Persistent MCP server definitions. Secrets are referenced by env name, never stored."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / "mcp.json"
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

    def import_server(self, source_path: str, *, replace: bool = False) -> Dict[str, Any]:
        """Import one portable local MCP definition without importing approval state."""
        candidate = Path(source_path).expanduser()
        if candidate.is_symlink():
            raise ValidationError("MCP import source must not be a symlink")
        source = candidate.resolve()
        if not source.is_file():
            raise ValidationError("MCP import source must be a regular JSON file")
        if source.suffix.lower() in {".mcpb", ".dxt"}:
            return self._import_bundle(source, replace=replace)
        try:
            if source.stat().st_size > MAX_IMPORT_BYTES:
                raise ValidationError("MCP import file exceeds 1 MiB")
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid MCP import file: {exc}") from exc
        spec = self._import_spec(value)
        name = str(spec.get("name", "")).strip()
        existing = {item["name"] for item in self.registry.list_servers()}
        if name in existing and not replace:
            raise ValidationError(f"MCP server already exists: {name}")
        if spec.get("transport", "stdio") == "stdio":
            if spec.get("command") == "${ZAGENT_PYTHON}":
                spec["command"] = sys.executable
            spec["cwd"] = self._resolve_import_path(source.parent, spec.get("cwd"), directory=True)
            spec["args"] = [
                self._resolve_import_argument(source.parent, str(argument))
                for argument in spec.get("args", [])
            ]
        # Approval is a local user decision and must never arrive from a package.
        spec["approved"] = False
        return self.add_server(spec)

    def _import_bundle(self, source: Path, *, replace: bool) -> Dict[str, Any]:
        if not zipfile.is_zipfile(source):
            raise ValidationError("MCPB/DXT package must be a ZIP archive")
        install_root = self.registry.data_dir / "mcp-bundles"
        install_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=install_root))
        package = staging / "package"
        try:
            self._extract_bundle(source, package)
            manifest_path = package / "manifest.json"
            if not manifest_path.is_file():
                children = [item for item in package.iterdir() if item.is_dir()]
                if len(children) == 1 and (children[0] / "manifest.json").is_file():
                    package = children[0]
                    manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            spec = self._bundle_spec(package, manifest)
            name = spec["name"]
            existing = {item["name"] for item in self.registry.list_servers()}
            if name in existing and not replace:
                raise ValidationError(f"MCP server already exists: {name}")
            target = install_root / name
            if target.exists() and not replace:
                raise ValidationError(f"MCP bundle already exists: {name}")
            backup = staging / "previous"
            if target.exists():
                target.replace(backup)
            package.replace(target)
            spec = self._bundle_spec(target, manifest)
            server = self.add_server(spec)
            if backup.exists():
                shutil.rmtree(backup)
            return {**server, "bundle_format": "mcpb", "bundle_version": manifest["manifest_version"]}
        except (OSError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise ValidationError(f"invalid MCP bundle: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _extract_bundle(source: Path, target: Path) -> None:
        target.mkdir(parents=True)
        total = 0
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_BUNDLE_FILES:
                raise ValidationError("MCP bundle contains too many files")
            for member in members:
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
                    raise ValidationError("MCP bundle contains an unsafe path")
                total += member.file_size
                if total > MAX_BUNDLE_BYTES:
                    raise ValidationError("MCP bundle exceeds 128 MiB")
                destination = target.joinpath(*path.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as reader, destination.open("wb") as writer:
                        shutil.copyfileobj(reader, writer)

    @classmethod
    def _bundle_spec(cls, root: Path, manifest: Any) -> Dict[str, Any]:
        if not isinstance(manifest, dict) or str(manifest.get("manifest_version")) not in {
            "0.1", "0.2", "0.3", "0.4"
        }:
            raise ValidationError("unsupported MCPB manifest_version")
        name = str(manifest.get("name", "")).strip()
        if not MCP_NAME_RE.fullmatch(name):
            raise ValidationError("invalid MCPB name")
        server = manifest.get("server")
        if not isinstance(server, dict):
            raise ValidationError("MCPB manifest requires server")
        runtime = str(server.get("type", ""))
        if runtime not in {"node", "python", "binary"}:
            raise ValidationError(f"unsupported MCPB server type: {runtime}")
        entry = cls._bundle_path(root, str(server.get("entry_point", "")))
        config = server.get("mcp_config", {})
        if not isinstance(config, dict):
            raise ValidationError("invalid MCPB mcp_config")
        defaults = cls._bundle_defaults(manifest.get("user_config", {}))
        raw_args = config.get("args", [])
        if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
            raise ValidationError("invalid MCPB argument list")
        args = [cls._substitute_bundle(item, root, defaults) for item in raw_args]
        entry_text = str(entry)
        if runtime in {"node", "python"} and not any(item == entry_text for item in args):
            args.insert(0, entry_text)
        if runtime == "python":
            command = sys.executable
        elif runtime == "node":
            command = shutil.which("node") or "node"
        else:
            command = entry_text
            if args and args[0] == entry_text:
                args.pop(0)
        return {
            "name": name,
            "transport": "stdio",
            "command": command,
            "args": args,
            "cwd": str(root),
            "env": [],
            "sandbox": True,
            "sandbox_read_roots": [str(root)],
            "sandbox_write_roots": [],
            "network": bool(manifest.get("privacy_policies")),
            "enabled": True,
            "approved": False,
        }

    @staticmethod
    def _bundle_path(root: Path, value: str) -> Path:
        candidate = (root / value).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            raise ValidationError("MCPB entry_point escapes the bundle or does not exist")
        return candidate

    @staticmethod
    def _bundle_defaults(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            str(name): config.get("default", "")
            for name, config in value.items()
            if isinstance(config, dict) and not config.get("sensitive", False)
        }

    @staticmethod
    def _substitute_bundle(value: str, root: Path, defaults: Dict[str, Any]) -> str:
        result = value.replace("${__dirname}", str(root))
        for name, default in defaults.items():
            replacement = str(default).lower() if isinstance(default, bool) else str(default)
            result = result.replace(f"${{user_config.{name}}}", replacement)
        result = result.replace("${HOME}", str(Path.home()))
        return re.sub(r"\$\{user_config\.[^}]+\}", "", result)

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

    @staticmethod
    def _import_spec(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("MCP import file must be a JSON object")
        if value.get("schema_version") == 1 and isinstance(value.get("server"), dict):
            return dict(value["server"])
        catalog = value.get("mcpServers")
        if not isinstance(catalog, dict):
            catalog = value.get("servers")
        if not isinstance(catalog, dict) or len(catalog) != 1:
            raise ValidationError(
                "MCP import requires one server in server, mcpServers, or servers"
            )
        name, raw = next(iter(catalog.items()))
        if not isinstance(raw, dict):
            raise ValidationError("MCP server definition must be an object")
        url = raw.get("url")
        if isinstance(url, str) and url:
            return {
                "name": name,
                "transport": "http",
                "url": url,
                "oauth": bool(raw.get("oauth", False)),
                "enabled": True,
                "approved": False,
            }
        env_value = raw.get("env", {})
        if env_value is not None and not isinstance(env_value, dict):
            raise ValidationError("standard MCP env must be an object")
        return {
            "name": name,
            "transport": "stdio",
            "command": raw.get("command", ""),
            "args": raw.get("args", []),
            "cwd": raw.get("cwd"),
            # Secret values from foreign configs are deliberately not persisted.
            # Only names already present in the Core environment are forwarded.
            "env": list((env_value or {}).keys()),
            "timeout_seconds": raw.get("timeout_seconds", 15),
            "sandbox": raw.get("sandbox", True),
            "network": raw.get("network", False),
            "enabled": raw.get("enabled", True),
            "approved": False,
        }

    @staticmethod
    def _resolve_import_path(root: Path, value: Any, *, directory: bool) -> Optional[str]:
        if value in (None, ""):
            return None
        candidate = Path(str(value)).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if root != resolved and root not in resolved.parents:
            raise ValidationError("relative MCP import path escapes the package directory")
        if directory and not resolved.is_dir():
            raise ValidationError(f"MCP import working directory does not exist: {value}")
        return str(resolved)

    @classmethod
    def _resolve_import_argument(cls, root: Path, value: str) -> str:
        if not value.startswith(("./", "../")):
            return value
        resolved = cls._resolve_import_path(root, value, directory=False)
        assert resolved is not None
        if not Path(resolved).is_file():
            raise ValidationError(f"MCP import argument file does not exist: {value}")
        return resolved

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
