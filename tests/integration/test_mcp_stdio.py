from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zagent.agent.mcp_tools import MCPToolExecutor
from zagent.agent.runtime import AgentRuntime
from zagent.domain.errors import ValidationError
from zagent.domain.models import ModelResponse, ToolCall
from zagent.extensions.mcp import MCPConfigRegistry, MCPManager
from zagent.security.permissions import PermissionBroker
from zagent.storage import SqliteStore

SERVER = Path(__file__).parents[1] / "fixtures" / "mcp_echo_server.py"


def test_real_stdio_server_initialize_list_call_and_restart(tmp_path):
    data_dir = tmp_path / "data"
    manager = MCPManager(MCPConfigRegistry(str(data_dir)))
    try:
        configured = manager.add_server(
            {
                "name": "real-echo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER)],
                "enabled": True,
                "approved": True,
                "sandbox": False,
            }
        )
        assert configured["status"] == "ready"
        connected = manager.connect("real-echo")
        assert connected["protocol_version"] == "2025-11-25"
        assert connected["server_info"]["name"] == "zagent-test-echo"
        assert manager.list_servers()[0]["status"] == "connected"

        tools = manager.list_tools("real-echo")
        assert tools[0]["name"] == "echo"
        result = manager.call_tool("real-echo", "echo", {"text": "真实 MCP 调用"})
        assert result["structuredContent"] == {"echo": "真实 MCP 调用"}
        assert manager.disconnect("real-echo")

        # The process is recreated from persisted configuration, not an in-memory mock.
        restarted = MCPManager(MCPConfigRegistry(str(data_dir)))
        try:
            assert restarted.list_tools("real-echo")[0]["name"] == "echo"
        finally:
            restarted.close()
    finally:
        manager.close()


def test_stdio_execution_requires_enabled_and_explicit_approval(tmp_path):
    manager = MCPManager(MCPConfigRegistry(str(tmp_path)))
    try:
        manager.add_server(
            {
                "name": "not-approved",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER)],
            }
        )
        with pytest.raises(ValidationError, match="explicit approval"):
            manager.connect("not-approved")
        manager.set_state("not-approved", approved=True, enabled=False)
        with pytest.raises(ValidationError, match="disabled"):
            manager.connect("not-approved")
    finally:
        manager.close()


def test_approved_mcp_tool_is_exposed_to_native_agent_tool_loop(tmp_path):
    manager = MCPManager(MCPConfigRegistry(str(tmp_path)))
    try:
        manager.add_server(
            {
                "name": "agent-echo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER)],
                "approved": True,
                "sandbox": False,
            }
        )
        permission_store = SqliteStore(str(tmp_path / "permissions"))
        broker = PermissionBroker(permission_store)
        executor = MCPToolExecutor(manager, broker)
        schema = executor.schemas[0]
        alias = schema["function"]["name"]
        assert alias == "mcp_agent-echo_echo"
        assert schema["function"]["parameters"]["required"] == ["text"]
        broker.approve_inline_once(
            None, "mcp", "agent-echo", "tool:echo", {"text": "模型工具链"}
        )
        result = executor.execute("", alias, {"text": "模型工具链"})
        assert result["mcp_server"] == "agent-echo"
        assert result["result"]["structuredContent"]["echo"] == "模型工具链"
        permission_store.close()
    finally:
        manager.close()


def test_native_agent_loop_invokes_real_mcp_subprocess(tmp_path, store, session_id, context):
    manager = MCPManager(MCPConfigRegistry(str(tmp_path / "mcp-data")))

    class MCPSequenceProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.seen_tools = []
            self.seen_messages = []

        def complete(self, messages, tools):
            self.calls += 1
            self.seen_tools.append(tools)
            self.seen_messages.append(messages)
            if self.calls == 1:
                return ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCall("mcp_call_1", "mcp_loop-echo_echo", {"text": "Agent loop"})
                    ],
                )
            return ModelResponse(content="MCP 工具调用完成")

    try:
        manager.add_server(
            {
                "name": "loop-echo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER)],
                "approved": True,
                "sandbox": False,
            }
        )
        provider = MCPSequenceProvider()
        broker = PermissionBroker(store)
        broker.approve_inline_once(
            session_id, "mcp", "loop-echo", "tool:echo", {"text": "Agent loop"}
        )
        runtime = AgentRuntime(store, context, provider, MCPToolExecutor(manager, broker))
        result = runtime.send(session_id, "请调用 MCP echo")
        assert result.final_event.payload == "MCP 工具调用完成"
        assert provider.seen_tools[0][0]["function"]["name"] == "mcp_loop-echo_echo"
        assert provider.seen_messages[1][-1]["role"] == "tool"
        tool_event = next(
            event for event in store.list_events(session_id) if event.tool_name == "mcp_loop-echo_echo"
        )
        assert tool_event.payload["mcp_server"] == "loop-echo"
        assert tool_event.payload["result"]["structuredContent"] == {"echo": "Agent loop"}
    finally:
        manager.close()
