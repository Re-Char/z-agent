from __future__ import annotations

import hashlib
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from zagent.domain.errors import ValidationError
from zagent.security import PermissionBroker

from .manifest import ExtensionManifest, ExtensionRegistry
from .mcp_client import MCPStdioClient

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


class ExtensionHostManager:
    """Owns isolated extension worker processes; extensions never load in Core."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        permissions: PermissionBroker,
        data_dir: str,
        *,
        sandbox_enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._state_root = Path(data_dir) / "extension-state"
        self._sandbox_enabled = sandbox_enabled
        self._clients: Dict[str, MCPStdioClient] = {}
        self._lock = threading.RLock()

    def status(self, extension_id: str) -> Dict[str, Any]:
        manifest = self._registry.get(extension_id)
        with self._lock:
            client = self._clients.get(extension_id)
        return {
            "extension_id": extension_id,
            "enabled": manifest.enabled,
            "runtime": manifest.runtime,
            "connected": bool(client and client.connected),
            "sandbox": "required",
        }

    def connect(
        self, extension_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        manifest = self._registry.get(extension_id)
        self._validate_executable_manifest(manifest)
        start_arguments = {
            "runtime": manifest.runtime,
            "version": manifest.version,
            "permissions": manifest.permissions,
            "package_sha256": manifest.package_sha256,
        }
        self._permissions.require(
            session_id,
            "extension",
            extension_id,
            "host:start",
            start_arguments,
            {
                "extension": extension_id,
                "runtime": manifest.runtime,
                "permissions": manifest.permissions,
                "sandbox": "required",
            },
        )
        with self._lock:
            client = self._clients.get(extension_id)
            if client is None:
                client = self._create_client(manifest)
                self._clients[extension_id] = client
            try:
                info = client.connect()
            except Exception:
                self._clients.pop(extension_id, None)
                raise
        return {"extension_id": extension_id, **info}

    def list_tools(
        self, extension_id: str, session_id: Optional[str] = None
    ) -> list[Dict[str, Any]]:
        return self._client(extension_id, session_id).list_tools()

    def call_tool(
        self,
        extension_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._permissions.require(
            session_id,
            "extension",
            extension_id,
            f"tool:{tool_name}",
            arguments,
            {"extension": extension_id, "tool": tool_name},
        )
        return self._client(extension_id, session_id).call_tool(tool_name, arguments)

    def disconnect(self, extension_id: str) -> bool:
        with self._lock:
            client = self._clients.pop(extension_id, None)
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

    def _client(self, extension_id: str, session_id: Optional[str]) -> MCPStdioClient:
        with self._lock:
            client = self._clients.get(extension_id)
        if client is None or not client.connected:
            self.connect(extension_id, session_id)
            with self._lock:
                client = self._clients[extension_id]
        return client

    def _create_client(self, manifest: ExtensionManifest) -> MCPStdioClient:
        assert manifest.entry is not None
        extension_root = Path(manifest.root).resolve()
        state_root = self._state_root / manifest.extension_id
        state_root.mkdir(parents=True, exist_ok=True)
        package_root = Path(__file__).resolve().parents[2]
        if manifest.runtime == "python":
            executable = sys.executable
            python_host = Path(__file__).with_name("python_host.py")
            args = [
                str(python_host),
                "--root",
                str(extension_root),
                "--entry",
                manifest.entry,
            ]
            env = []
        else:
            executable = shutil.which("node") or "node"
            node_host = Path(__file__).with_name("node_host.cjs")
            args = [
                str(node_host),
                "--root",
                str(extension_root),
                "--entry",
                manifest.entry,
            ]
            env = []
        return MCPStdioClient(
            executable,
            args,
            cwd=str(extension_root),
            env_names=env,
            timeout_seconds=15,
            sandbox=self._sandbox_enabled,
            sandbox_read_roots=[str(extension_root), str(package_root)],
            sandbox_write_roots=[str(state_root)],
            network="network" in manifest.permissions,
        )

    @staticmethod
    def _validate_executable_manifest(manifest: ExtensionManifest) -> None:
        if not manifest.enabled:
            raise ValidationError("extension is disabled")
        if manifest.runtime not in {"python", "node"}:
            raise ValidationError("declarative extensions do not require an Extension Host")
        if not manifest.entry:
            raise ValidationError("executable extension requires an entry")
        if manifest.status != "installed" or manifest.signature_status != "verified":
            raise ValidationError(f"extension integrity status blocks execution: {manifest.status}")


def extension_tool_alias(extension_id: str, tool_name: str) -> str:
    raw = f"ext_{extension_id}_{tool_name}"
    normalized = _UNSAFE_NAME.sub("_", raw).strip("_") or "extension_tool"
    if len(normalized) <= 64:
        return normalized
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[:53]}_{suffix}"
