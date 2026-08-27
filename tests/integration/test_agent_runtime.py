from zagent.agent.runtime import AgentRuntime, AgentRuntimeLimits
from zagent.agent.tools import ContextToolExecutor
from zagent.domain.errors import AgentLimitError
from zagent.domain.models import ModelResponse, ToolCall


class SequenceProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        return next(self.responses)


def test_agent_executes_context_tool_then_finishes(store, session_id, context):
    provider = SequenceProvider([
        ModelResponse(content="", tool_calls=[ToolCall("call_1", "context_status", {})], raw={"id": "raw-1"}),
        ModelResponse(content="任务状态已检查", usage={"total_tokens": 20}, raw={"id": "raw-2"}),
    ])
    runtime = AgentRuntime(store, context, provider, ContextToolExecutor(context))
    result = runtime.send(session_id, "检查当前状态")
    assert result.final_event.payload == "任务状态已检查"
    assert result.tool_rounds == 1
    events = store.list_events(session_id)
    assert any(event.kind == "tool_result" and event.tool_name == "context_status" for event in events)
    assert provider.messages[1][-1]["role"] == "tool"


def test_agent_stops_at_tool_round_limit(store, session_id, context):
    repeated = [
        ModelResponse(content="", tool_calls=[ToolCall(f"call_{index}", "context_status", {})])
        for index in range(3)
    ]
    runtime = AgentRuntime(
        store,
        context,
        SequenceProvider(repeated),
        ContextToolExecutor(context),
        AgentRuntimeLimits(max_tool_rounds=1, task_timeout_seconds=10),
    )
    import pytest
    with pytest.raises(AgentLimitError):
        runtime.send(session_id, "循环")
