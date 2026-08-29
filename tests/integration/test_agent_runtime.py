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
    with pytest.raises(AgentLimitError) as caught:
        runtime.send(session_id, "循环")

    checkpoint = caught.value.checkpoint
    assert checkpoint is not None
    assert checkpoint["reason"] == "max_tool_rounds"
    assert checkpoint["state"]["tool_rounds_completed"] == 1
    assert checkpoint["state"]["pending"] == [{"call_id": "call_1", "tool": "context_status"}]
    evidence = checkpoint["state"]["completed"][0]
    assert evidence["tool"] == "context_status"
    assert store.get_event(evidence["tool_result_event_id"]).kind == "tool_result"
    assert store.latest_checkpoint(session_id)["checkpoint_id"] == checkpoint["checkpoint_id"]

    working = context.build_working_set(session_id)
    assert checkpoint["checkpoint_id"] in working.messages[0]["content"]
    assert all(message.get("content") != str(checkpoint) for message in working.messages[1:])

    continuation_provider = SequenceProvider([ModelResponse(content="继续后已完成")])
    continuation = AgentRuntime(
        store, context, continuation_provider, ContextToolExecutor(context)
    )
    result = continuation.send(session_id, "继续任务")
    assert result.final_event.payload == "继续后已完成"
    assert checkpoint["checkpoint_id"] in continuation_provider.messages[0][0]["content"]
    assert store.latest_checkpoint(session_id, active_only=True) is None
    assert store.latest_checkpoint(session_id)["resolution_event_id"] == result.final_event.event_id


def test_agent_checkpoint_file_versions_are_evidence_backed(store, session_id, context):
    class FileToolExecutor:
        schemas = []

        def execute(self, _session_id, _name, _arguments):
            return {"path": "src/main.py", "sha256": "a" * 64, "ok": True}

    provider = SequenceProvider([
        ModelResponse(content="", tool_calls=[ToolCall("write_1", "fs_write", {})]),
        ModelResponse(content="", tool_calls=[ToolCall("read_2", "fs_read", {})]),
    ])
    runtime = AgentRuntime(
        store,
        context,
        provider,
        FileToolExecutor(),
        AgentRuntimeLimits(max_tool_rounds=1, task_timeout_seconds=10),
    )

    import pytest
    with pytest.raises(AgentLimitError) as caught:
        runtime.send(session_id, "写入并检查代码")

    version = caught.value.checkpoint["state"]["file_versions"][0]
    assert version["path"] == "src/main.py"
    assert version["sha256"] == "a" * 64
    assert store.get_event(version["evidence_event_id"]).tool_name == "fs_write"


def test_agent_preserves_reasoning_content_across_tool_round(store, session_id, context):
    reasoning = "先确认状态，再汇总"
    provider = SequenceProvider([
        ModelResponse(
            content="",
            tool_calls=[ToolCall("call_1", "context_status", {})],
            reasoning_content=reasoning,
            raw={"id": "raw-1"},
        ),
        ModelResponse(content="状态已汇总", usage={"total_tokens": 20}, raw={"id": "raw-2"}),
    ])
    runtime = AgentRuntime(store, context, provider, ContextToolExecutor(context))
    result = runtime.send(session_id, "检查状态")
    assert result.final_event.payload == "状态已汇总"
    # The continuation request must carry the original reasoning verbatim so
    # DeepSeek thinking-mode tool calls do not fail with 400 on the next round.
    tool_message = provider.messages[1][-2]
    assert tool_message["role"] == "assistant"
    assert tool_message["reasoning_content"] == reasoning
    assert tool_message["tool_calls"][0]["id"] == "call_1"


def test_agent_stores_final_reasoning_for_ui_but_excludes_it_from_working_set(
    store, session_id, context
):
    reasoning = "先比较两种方案，再给出结论"
    runtime = AgentRuntime(
        store,
        context,
        SequenceProvider([ModelResponse(content="采用方案 A", reasoning_content=reasoning)]),
        ContextToolExecutor(context),
    )

    result = runtime.send(session_id, "选择方案")

    events = store.list_events(session_id)
    reasoning_event = next(event for event in events if event.kind == "assistant_reasoning")
    assert reasoning_event.payload == reasoning
    assert reasoning_event.event_id not in result.working_set.included_event_ids
    assert all(message.get("content") != reasoning for message in result.working_set.messages)


def test_agent_aggregates_token_stats(store, session_id, context):
    provider = SequenceProvider([
        ModelResponse(content="", tool_calls=[ToolCall("call_1", "context_status", {})], usage={
            "total_tokens": 100, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 25,
        }),
        ModelResponse(content="统计完成", usage={
            "total_tokens": 120, "completion_tokens": 15,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 25,
        }),
    ])
    runtime = AgentRuntime(store, context, provider, ContextToolExecutor(context))
    result = runtime.send(session_id, "汇总")
    stats = result.stats
    assert stats.total_tokens == 220
    assert stats.completion_tokens == 20
    assert stats.cache_hit_tokens == 150
    assert stats.cache_miss_tokens == 50
    assert stats.cache_hit_rate == 75.0
    assert stats.elapsed_seconds >= 0
    assert stats.tokens_per_second >= 0
    assert result.to_dict()["stats"]["cache_hit_rate"] == 75.0
