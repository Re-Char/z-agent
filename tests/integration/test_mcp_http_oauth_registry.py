from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from zagent.agent.mcp_tools import MCPToolExecutor
from zagent.domain.errors import ValidationError
from zagent.extensions.mcp_http import MCPStreamableHTTPClient
from zagent.extensions.oauth import MCPOAuthManager
from zagent.extensions.registry_client import MCPRegistryClient
from zagent.security.permissions import PermissionBroker
from zagent.security.secrets import SecretStore
from zagent.storage import SqliteStore


def test_streamable_http_json_sse_session_and_bearer_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "http-test", "version": "1"},
                    },
                },
            )
        assert request.headers["mcp-session-id"] == "session-1"
        assert request.headers["mcp-protocol-version"] == "2025-11-25"
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [{"name": "echo", "inputSchema": {"type": "object"}}]
                    },
                }
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {payload}\n\n",
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"structuredContent": body["params"]["arguments"]},
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = MCPStreamableHTTPClient(
        "http://localhost/mcp", token_provider=lambda: "access-token", client=http
    )
    assert client.connect()["session_id"] == "session-1"
    assert client.list_tools()[0]["name"] == "echo"
    assert client.call_tool("echo", {"中文": "成功"})["structuredContent"] == {"中文": "成功"}
    client.close()
    assert all(request.headers["authorization"] == "Bearer access-token" for request in seen)
    assert seen[-1].method == "DELETE"


def test_remote_http_and_private_literal_endpoints_are_rejected():
    with pytest.raises(ValidationError, match="must use HTTPS"):
        MCPStreamableHTTPClient("http://example.com/mcp")
    with pytest.raises(ValidationError, match="private or reserved"):
        MCPStreamableHTTPClient("https://169.254.169.254/mcp")


def test_oauth_discovery_pkce_resource_and_token_storage(tmp_path):
    token_form = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={
                    "resource": "http://localhost",
                    "authorization_servers": ["https://auth.example"],
                    "scopes_supported": ["mcp:tools"],
                },
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url.path == "/token":
            token_form.update(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "secret-token", "expires_in": 3600})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    secrets_store = SecretStore(str(tmp_path))
    oauth = MCPOAuthManager(str(tmp_path), secrets_store, client=client)
    started = oauth.begin(
        "remote", "http://localhost/mcp", "public-client", "http://localhost:9123/callback", []
    )
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["http://localhost"]
    assert query["state"] == [started["state"]]
    assert oauth.complete(started["state"], "authorization-code")["authorized"] is True
    assert token_form["code_verifier"]
    assert token_form["resource"] == ["http://localhost"]
    assert oauth.access_token("remote") == "secret-token"


def test_registry_search_detail_and_safe_remote_import():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0.1/servers":
            assert request.url.params["version"] == "latest"
            return httpx.Response(200, json={"servers": [{"name": "io.example/echo"}], "metadata": {}})
        return httpx.Response(
            200,
            json={
                "server": {
                    "name": "io.example/echo",
                    "version": "1.2.3",
                    "remotes": [{"type": "streamable-http", "url": "https://mcp.example/mcp"}],
                }
            },
        )

    registry = MCPRegistryClient(
        "https://registry.example", httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert registry.search("echo")["servers"][0]["name"] == "io.example/echo"
    config = registry.remote_config("io.example/echo")
    assert config["transport"] == "http"
    assert config["approved"] is False
    assert config["url"] == "https://mcp.example/mcp"


def test_approved_streamable_http_tools_are_exposed_to_agent(tmp_path):
    class ConnectedHttpManager:
        def list_servers(self):
            return [
                {
                    "name": "official-docs",
                    "transport": "http",
                    "enabled": True,
                    "approved": True,
                }
            ]

        def list_tools(self, name):
            assert name == "official-docs"
            return [
                {
                    "name": "search_docs",
                    "description": "Search official MCP documentation",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]

        def call_tool(self, name, tool, arguments):
            return {
                "content": [{"type": "text", "text": arguments["query"]}],
                "structuredContent": {"server": name, "tool": tool},
                "isError": False,
            }

    store = SqliteStore(str(tmp_path / "permissions"))
    broker = PermissionBroker(store)
    executor = MCPToolExecutor(ConnectedHttpManager(), broker)  # type: ignore[arg-type]
    try:
        schema = executor.schemas[0]
        assert schema["function"]["name"] == "mcp_official-docs_search_docs"
        arguments = {"query": "Streamable HTTP"}
        broker.approve_inline_once(
            None, "mcp", "official-docs", "tool:search_docs", arguments
        )
        result = executor.execute("", "mcp_official-docs_search_docs", arguments)
        assert result["result"]["structuredContent"]["server"] == "official-docs"
    finally:
        store.close()
