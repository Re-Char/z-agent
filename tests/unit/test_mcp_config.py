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

