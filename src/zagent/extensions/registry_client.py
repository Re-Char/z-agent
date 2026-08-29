from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from zagent.domain.errors import ValidationError

OFFICIAL_REGISTRY_URL = "https://registry.modelcontextprotocol.io"


class MCPRegistryClient:
    """Read-only client for the official MCP Registry v0.1 API."""

    def __init__(
        self,
        base_url: str = OFFICIAL_REGISTRY_URL,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=20, follow_redirects=False)
        self._owns_client = client is None

    def search(
        self, query: str = "", *, limit: int = 20, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValidationError("registry limit must be between 1 and 100")
        params: Dict[str, Any] = {"limit": limit, "version": "latest"}
        if query.strip():
            params["search"] = query.strip()
        if cursor:
            params["cursor"] = cursor
        return self._get("/v0.1/servers", params=params)

    def get_version(self, server_name: str, version: str = "latest") -> Dict[str, Any]:
        if not server_name or len(server_name) > 500 or not version or len(version) > 128:
            raise ValidationError("invalid MCP Registry server name or version")
        name = quote(server_name, safe="")
        selected = quote(version, safe="")
        return self._get(f"/v0.1/servers/{name}/versions/{selected}")

    def remote_config(
        self, server_name: str, version: str = "latest", local_name: Optional[str] = None
    ) -> Dict[str, Any]:
        response = self.get_version(server_name, version)
        record = response.get("server", response)
        if not isinstance(record, dict):
            raise ValidationError("registry returned an invalid server record")
        remotes = record.get("remotes", [])
        if not isinstance(remotes, list):
            remotes = []
        remote = next(
            (
                item
                for item in remotes
                if isinstance(item, dict)
                and str(item.get("type", "")).lower()
                in {"streamable-http", "streamable_http", "http"}
                and isinstance(item.get("url"), str)
            ),
            None,
        )
        if remote is None:
            raise ValidationError(
                "registry entry has no Streamable HTTP remote; package installation is not automatic"
            )
        name = local_name or self._local_name(server_name)
        return {
            "name": name,
            "transport": "http",
            "url": remote["url"],
            "timeout_seconds": 30,
            "oauth": bool(remote.get("authorization")),
            "oauth_scopes": [],
            "enabled": True,
            "approved": False,
            "registry": {
                "name": server_name,
                "version": record.get("version", version),
                "source": self.base_url,
            },
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise ValidationError(f"MCP Registry request failed: {exc}") from exc
        if response.is_redirect:
            raise ValidationError("MCP Registry redirect refused")
        if response.status_code >= 400:
            raise ValidationError(f"MCP Registry returned status {response.status_code}")
        try:
            value = response.json()
        except ValueError as exc:
            raise ValidationError("MCP Registry returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("MCP Registry returned invalid JSON")
        return value

    @staticmethod
    def _local_name(value: str) -> str:
        normalized = "".join(
            char.lower() if char.isalnum() or char in "._-" else "-" for char in value
        ).strip("-.")
        return (normalized or "registry-server")[:128]
