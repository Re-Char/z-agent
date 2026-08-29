from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from zagent.extensions.host import ExtensionHostManager
from zagent.extensions.manifest import ExtensionRegistry
from zagent.security.permissions import PermissionBroker
from zagent.storage import SqliteStore


def test_signed_python_extension_runs_in_independent_host_with_permissions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "zagent.extension.json").write_text(
        json.dumps(
            {
                "id": "com.example.process",
                "name": "Process test",
                "version": "1.0.0",
                "runtime": "python",
                "entry": "extension.py",
                "contributes": ["tools"],
                "permissions": [],
            }
        ),
        encoding="utf-8",
    )
    (source / "extension.py").write_text(
        "TOOLS = [{'name': 'echo', 'inputSchema': {'type': 'object'}}]\n"
        "def invoke(name, arguments):\n"
        "    return {'echo': arguments, 'host_pid': __import__('os').getpid()}\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    registry = ExtensionRegistry(str(data_dir))
    manifest = registry.import_extension(str(source), enabled=True)
    assert manifest.signature_status == "verified"
    assert Path(manifest.sbom_path or "").is_file()
    store = SqliteStore(str(data_dir))
    broker = PermissionBroker(store)
    hosts = ExtensionHostManager(registry, broker, str(data_dir), sandbox_enabled=False)
    try:
        start_arguments = {
            "runtime": manifest.runtime,
            "version": manifest.version,
            "permissions": manifest.permissions,
            "package_sha256": manifest.package_sha256,
        }
        broker.approve_inline_once(
            None, "extension", manifest.extension_id, "host:start", start_arguments
        )
        connected = hosts.connect(manifest.extension_id)
        assert connected["server_info"]["pid"] != os.getpid()
        assert hosts.list_tools(manifest.extension_id)[0]["name"] == "echo"
        arguments = {"text": "独立进程"}
        broker.approve_inline_once(
            None, "extension", manifest.extension_id, "tool:echo", arguments
        )
        result = hosts.call_tool(manifest.extension_id, "echo", arguments)
        assert result["structuredContent"]["echo"] == arguments
        assert result["structuredContent"]["host_pid"] != os.getpid()
    finally:
        hosts.close()
        store.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runtime is unavailable")
def test_signed_node_extension_runs_in_independent_host(tmp_path):
    source = tmp_path / "node-source"
    source.mkdir()
    (source / "zagent.extension.json").write_text(
        json.dumps(
            {
                "id": "com.example.node-process",
                "version": "1.0.0",
                "runtime": "node",
                "entry": "extension.cjs",
                "contributes": ["tools"],
                "permissions": [],
            }
        ),
        encoding="utf-8",
    )
    (source / "extension.cjs").write_text(
        "exports.tools=[{name:'echo',inputSchema:{type:'object'}}];\n"
        "exports.invoke=async (name,args)=>({echo:args,host_pid:process.pid});\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "node-data"
    registry = ExtensionRegistry(str(data_dir))
    manifest = registry.import_extension(str(source), enabled=True)
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
        connected = hosts.connect(manifest.extension_id)
        assert connected["server_info"]["pid"] != os.getpid()
        arguments = {"runtime": "node"}
        broker.approve_inline_once(
            None, "extension", manifest.extension_id, "tool:echo", arguments
        )
        result = hosts.call_tool(manifest.extension_id, "echo", arguments)
        assert result["structuredContent"]["echo"] == arguments
    finally:
        hosts.close()
        store.close()
