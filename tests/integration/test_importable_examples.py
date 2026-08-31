from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from zagent.domain.errors import ValidationError
from zagent.extensions.host import ExtensionHostManager
from zagent.extensions.manifest import ExtensionRegistry
from zagent.extensions.mcp import MCPConfigRegistry, MCPManager
from zagent.security.permissions import PermissionBroker
from zagent.storage import SqliteStore

EXAMPLES = Path(__file__).parents[2] / "examples" / "integrations"


def test_importable_extension_zip_runs_real_python_host(tmp_path):
    data_dir = tmp_path / "extension-data"
    registry = ExtensionRegistry(str(data_dir))
    manifest = registry.import_extension(
        str(EXAMPLES / "zagent-demo-extension.zip"), enabled=True
    )
    assert manifest.extension_id == "com.zagent.demo"
    assert manifest.signature_status == "verified"
    assert Path(manifest.sbom_path or "").is_file()

    store = SqliteStore(str(data_dir))
    broker = PermissionBroker(store)
    hosts = ExtensionHostManager(registry, broker, str(data_dir), sandbox_enabled=False)
    try:
        broker.approve_inline_once(
            None,
            "extension",
            manifest.extension_id,
            "host:start",
            {
                "runtime": manifest.runtime,
                "version": manifest.version,
                "permissions": manifest.permissions,
                "package_sha256": manifest.package_sha256,
            },
        )
        hosts.connect(manifest.extension_id)
        assert [tool["name"] for tool in hosts.list_tools(manifest.extension_id)] == [
            "hello",
            "sum_numbers",
        ]
        arguments = {"name": "中文用户"}
        broker.approve_inline_once(
            None, "extension", manifest.extension_id, "tool:hello", arguments
        )
        result = hosts.call_tool(manifest.extension_id, "hello", arguments)
        assert result["structuredContent"]["message"] == "你好，中文用户！"
    finally:
        hosts.close()
        store.close()


def test_importable_mcp_config_resolves_runtime_and_calls_real_server(tmp_path):
    manager = MCPManager(MCPConfigRegistry(str(tmp_path / "mcp-data")))
    try:
        imported = manager.import_server(
            str(EXAMPLES / "mcp-demo" / "zagent-demo-mcp.json")
        )
        assert imported["approved"] is False
        assert imported["command"] == sys.executable
        assert imported["cwd"] == str((EXAMPLES / "mcp-demo").resolve())
        assert imported["args"] == [str((EXAMPLES / "mcp-demo" / "server.py").resolve())]

        # This test suite itself runs inside Codex's sandbox, so a second
        # sandbox-exec layer is unavailable. Keep the shipped example fail-closed,
        # but disable only the nested layer for the real protocol call below.
        manager.add_server({**imported, "approved": True, "sandbox": False})
        connected = manager.connect("zagent-demo-mcp")
        assert connected["protocol_version"] == "2025-11-25"
        assert connected["server_info"]["name"] == "zagent-demo-mcp"
        assert [tool["name"] for tool in manager.list_tools("zagent-demo-mcp")] == [
            "echo",
            "sum_numbers",
        ]
        result = manager.call_tool(
            "zagent-demo-mcp", "echo", {"text": "真实 MCP 已连通"}
        )
        assert result["structuredContent"]["echo"] == "真实 MCP 已连通"
    finally:
        manager.close()


def test_mcp_import_is_unapproved_and_rejects_path_escape(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    config = package / "escape.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "server": {
                    "name": "escape",
                    "transport": "stdio",
                    "command": "${ZAGENT_PYTHON}",
                    "args": ["../outside.py"],
                    "approved": True,
                },
            }
        ),
        encoding="utf-8",
    )
    manager = MCPManager(MCPConfigRegistry(str(tmp_path / "data")))
    try:
        with pytest.raises(ValidationError, match="escapes"):
            manager.import_server(str(config))
        assert manager.list_servers() == []
    finally:
        manager.close()


def test_imports_claude_desktop_standard_remote_config(tmp_path):
    manager = MCPManager(MCPConfigRegistry(str(tmp_path / "data")))
    try:
        server = manager.import_server(
            str(EXAMPLES / "marketplace" / "official-mcp-docs.json")
        )
        assert server["name"] == "official-mcp-docs"
        assert server["transport"] == "http"
        assert server["url"] == "https://modelcontextprotocol.io/mcp"
        assert server["approved"] is False
    finally:
        manager.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runtime is unavailable")
def test_imports_and_runs_official_mcpb_reference_extension(tmp_path):
    manager = MCPManager(MCPConfigRegistry(str(tmp_path / "data")))
    try:
        imported = manager.import_server(
            str(EXAMPLES / "marketplace" / "official-hello-world-node.mcpb")
        )
        assert imported["name"] == "hello-world-node"
        assert imported["bundle_format"] == "mcpb"
        assert imported["bundle_version"] == "0.3"
        assert imported["approved"] is False
        assert "mcp-bundles/hello-world-node/server/index.js" in " ".join(imported["args"])

        # The shipped policy stays sandboxed. Disable only the second nested
        # sandbox layer imposed by this Codex test host.
        manager.add_server({**imported, "approved": True, "sandbox": False})
        connected = manager.connect("hello-world-node")
        assert connected["server_info"]["name"] == "hello-world-node"
        assert [tool["name"] for tool in manager.list_tools("hello-world-node")] == [
            "get_current_time"
        ]
        result = manager.call_tool("hello-world-node", "get_current_time", {})
        assert result["content"][0]["text"].startswith("The current time is:")
    finally:
        manager.close()
