from __future__ import annotations

import ipaddress
import json
from contextlib import suppress
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import httpx

from zagent import __version__
from zagent.domain.errors import ValidationError

from .mcp_client import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, MCPProtocolError


class MCPStreamableHTTPClient:
    """MCP Streamable HTTP client implementing JSON and SSE response modes."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 30.0,
        token_provider: Optional[Callable[[], str]] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("MCP Streamable HTTP requires an absolute HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValidationError("remote MCP Streamable HTTP endpoints must use HTTPS")
        if parsed.hostname:
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if address and not address.is_loopback and (
                address.is_private
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValidationError("MCP endpoint may not target a private or reserved IP")
        self.url = url
        self._timeout = timeout_seconds
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._owns_client = client is None
        self._next_id = 1
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.server_info: Dict[str, Any] = {}
        self.server_capabilities: Dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self.protocol_version is not None

    def connect(self) -> Dict[str, Any]:
        if self.connected:
            return self.connection_info()
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

    def connection_info(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "protocol_version": self.protocol_version,
            "server_info": self.server_info,
            "capabilities": self.server_capabilities,
            "session_id": self.session_id,
        }

    def list_tools(self) -> list[Dict[str, Any]]:
        self._require_connected()
        if "tools" not in self.server_capabilities:
            raise MCPProtocolError("MCP server did not advertise tools capability")
        tools: list[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen: set[str] = set()
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else None)
            page = result.get("tools", [])
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise MCPProtocolError("MCP tools/list returned an invalid tools array")
            tools.extend(page)
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return tools
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise MCPProtocolError("MCP tools/list returned an invalid pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._require_connected()
        if not name or len(name) > 128:
            raise ValidationError("invalid MCP tool name")
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        response = self._post(message)
        payload = self._response_payload(response, request_id)
        if "error" in payload:
            error = payload.get("error")
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPProtocolError(f"MCP {method} failed: {text}")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise MCPProtocolError(f"MCP {method} returned an invalid result")
        return result

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        response = self._post(message)
        if response.status_code != 202 and response.content:
            self._response_payload(response, None)

    def close(self) -> None:
        if self.session_id:
            with suppress(httpx.HTTPError):
                self._client.delete(self.url, headers=self._headers())
        self.session_id = None
        self.protocol_version = None
        self.server_info = {}
        self.server_capabilities = {}
        if self._owns_client:
            self._client.close()

    def _post(self, message: Dict[str, Any]) -> httpx.Response:
        try:
            response = self._client.post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPProtocolError(f"MCP HTTP transport failed: {exc}") from exc
        if response.status_code == 401:
            raise ValidationError("MCP OAuth authorization required (HTTP 401)")
        if response.is_redirect:
            raise MCPProtocolError("MCP endpoint redirects are refused")
        if response.status_code >= 400:
            raise MCPProtocolError(f"MCP HTTP request failed with status {response.status_code}")
        returned_session = response.headers.get("Mcp-Session-Id")
        if returned_session:
            self.session_id = returned_session
        return response

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self._token_provider and (token := self._token_provider()):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _response_payload(response: httpx.Response, request_id: Optional[int]) -> Dict[str, Any]:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        candidates: list[Any] = []
        if content_type == "application/json":
            try:
                candidates = [response.json()]
            except json.JSONDecodeError as exc:
                raise MCPProtocolError("MCP HTTP server returned invalid JSON") from exc
        elif content_type == "text/event-stream":
            for block in response.text.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
                )
                if data:
                    try:
                        candidates.append(json.loads(data))
                    except json.JSONDecodeError as exc:
                        raise MCPProtocolError("MCP SSE event contained invalid JSON") from exc
        elif response.status_code == 202 and not response.content:
            return {}
        else:
            raise MCPProtocolError(f"unsupported MCP HTTP content type: {content_type or 'missing'}")
        for payload in candidates:
            if isinstance(payload, dict) and (request_id is None or payload.get("id") == request_id):
                if payload.get("jsonrpc") != "2.0":
                    raise MCPProtocolError("MCP HTTP response is not JSON-RPC 2.0")
                return payload
        raise MCPProtocolError("MCP HTTP response did not contain the matching JSON-RPC result")

    def _require_connected(self) -> None:
        if not self.connected:
            raise MCPProtocolError("MCP HTTP client is not initialized")

    @staticmethod
    def _object(value: Any, name: str) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise MCPProtocolError(f"MCP initialize returned invalid {name}")
        return value
