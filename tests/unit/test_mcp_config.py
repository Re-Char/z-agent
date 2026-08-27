import json

from zagent.extensions.mcp import MCPConfigRegistry


def test_missing_mcp_config_is_empty(tmp_path):
    assert MCPConfigRegistry(str(tmp_path)).list_servers() == []


def test_mcp_stdio_and_http_are_normalized(tmp_path):
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": {
        "local": {"transport": "stdio", "command": "server", "enabled": True},
        "remote": {"transport": "http", "url": "https://mcp.example", "enabled": False}
    }}), encoding="utf-8")
    servers = MCPConfigRegistry(str(tmp_path)).list_servers()
    assert servers[0]["command"] == "server"
    assert servers[1]["url"] == "https://mcp.example"



def test_add_and_remove_mcp_server(tmp_path):
    import pytest

    from zagent.domain.errors import ValidationError

    registry = MCPConfigRegistry(str(tmp_path))
    server = registry.add_server({"name": "files", "transport": "stdio", "command": "npx",
                              "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]})
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    remote = registry.add_server({"name": "remote", "transport": "http",
                              "url": "https://mcp.example.com/sse", "enabled": False})
    assert remote["url"].startswith("https://")
    assert remote["enabled"] is False
    names = [item["name"] for item in registry.list_servers()]
    assert names == ["files", "remote"]
    assert registry.remove_server("files")
    assert [item["name"] for item in registry.list_servers()] == ["remote"]
    assert not registry.remove_server("missing")
    with pytest.raises(ValidationError):
        registry.add_server({"name": "bad", "transport": "stdio"})
    with pytest.raises(ValidationError):
        registry.add_server({"name": "bad", "transport": "http", "url": "ftp://x"})
