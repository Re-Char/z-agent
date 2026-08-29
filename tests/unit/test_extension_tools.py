from __future__ import annotations

from types import SimpleNamespace

import pytest

from zagent.agent.extension_tools import ExtensionToolExecutor
from zagent.domain.errors import ToolExecutionError


class FakeRegistry:
    def __init__(self, extensions):
        self.extensions = extensions

    def discover(self):
        return self.extensions


class FakeHosts:
    def __init__(self, fail=False):
        self.fail = fail

    def list_tools(self, extension_id):
        if self.fail:
            raise RuntimeError("offline")
        assert extension_id == "com.example.echo"
        return [
            {
                "name": "echo text",
                "description": "Echo",
                "inputSchema": {"type": "object", "required": ["text"]},
            },
            {"name": 42},
        ]

    def call_tool(self, extension_id, tool_name, arguments, session_id):
        assert (extension_id, tool_name, session_id) == (
            "com.example.echo",
            "echo text",
            "session-1",
        )
        return {"structuredContent": arguments, "isError": False}


def test_extension_tool_executor_discovers_alias_and_dispatches():
    extension = SimpleNamespace(
        enabled=True, runtime="python", extension_id="com.example.echo"
    )
    executor = ExtensionToolExecutor(FakeRegistry([extension]), FakeHosts())
    schemas = executor.schemas
    assert schemas[0]["function"]["name"] == "ext_com_example_echo_echo_text"
    result = executor.execute(
        "session-1", schemas[0]["function"]["name"], {"text": "中文"}
    )
    assert result["ok"] is True
    assert result["result"]["structuredContent"] == {"text": "中文"}


def test_extension_tool_executor_skips_unavailable_and_rejects_unknown():
    extensions = [
        SimpleNamespace(enabled=False, runtime="python", extension_id="disabled"),
        SimpleNamespace(enabled=True, runtime="declarative", extension_id="declarative"),
        SimpleNamespace(enabled=True, runtime="node", extension_id="com.example.echo"),
    ]
    executor = ExtensionToolExecutor(FakeRegistry(extensions), FakeHosts(fail=True))
    assert executor.schemas == []
    with pytest.raises(ToolExecutionError, match="unknown or unavailable"):
        executor.execute("session-1", "missing", {})
