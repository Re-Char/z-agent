from zagent.agent.runtime import AgentRuntime, AgentRuntimeLimits
from zagent.agent.tools import ContextToolExecutor
from zagent.domain.errors import AgentLimitError
from zagent.domain.models import ModelResponse, ToolCall
from zagent.security import PermissionBroker


class SequenceProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        return next(self.responses)


class CountingToolExecutor:
    schemas = []

    def __init__(self):
        self.calls = []

    def execute(self, _session_id, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "path": arguments.get("path", ""), "sha256": "b" * 64}


class StreamingSequenceProvider:
    def __init__(self, rounds):
        self.rounds = iter(rounds)

    def complete_stream(self, _messages, _tools):
        yield from next(self.rounds)


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


def test_stream_reports_bounded_tool_progress_without_arguments(store, session_id, context):
    tools = CountingToolExecutor()
    provider = StreamingSequenceProvider([
        [
            {
                "type": "tool_call", "index": 0, "call_id": "write_1",
                "name": "fs_write", "arguments_delta": "{very large source}",
            },
            {
                "type": "tool_call", "index": 0, "call_id": "write_1",
                "name": "fs_write", "arguments_delta": "more source",
            },
            {
                "type": "done",
                "response": ModelResponse(
                    content="",
                    tool_calls=[ToolCall("write_1", "fs_write", {"path": "index.html"})],
                ),
            },
        ],
        [
            {"type": "content", "text": "已完成"},
            {"type": "done", "response": ModelResponse(content="已完成")},
        ],
    ])
    runtime = AgentRuntime(store, context, provider, tools)

    events = list(runtime.send_stream(session_id, "生成 2048"))

    assert [item["type"] for item in events] == [
        "status", "status", "tool_call", "tool_result", "status", "content", "done"
    ]
    assert events[1]["message"] == "模型正在生成工具调用参数"
    progress = events[2]
    assert progress == {"type": "tool_call", "name": "fs_write", "call_id": "write_1"}
    assert "arguments_delta" not in progress
    assert tools.calls == [("fs_write", {"path": "index.html"})]


def test_stream_reports_tool_when_provider_only_names_it_at_completion(store, session_id, context):
    tools = CountingToolExecutor()
    provider = StreamingSequenceProvider([
        [{"type": "done", "response": ModelResponse(
            content="", tool_calls=[ToolCall("late_1", "fs_write", {"path": "game.py"})]
        )}],
        [{"type": "done", "response": ModelResponse(content="完成")}],
    ])
    runtime = AgentRuntime(store, context, provider, tools)

    events = list(runtime.send_stream(session_id, "生成 Python 2048"))

    assert {"type": "tool_call", "name": "fs_write", "call_id": "late_1"} in events
    assert {
        "type": "tool_result", "name": "fs_write", "ok": True, "detail": "game.py"
    } in events


def test_successful_file_task_creates_candidate_memory_but_chat_does_not(
    store, session_id, context
):
    tools = CountingToolExecutor()
    runtime = AgentRuntime(store, context, SequenceProvider([
        ModelResponse(
            content="", tool_calls=[ToolCall("write_1", "fs_write", {"path": "index.html"})]
        ),
        ModelResponse(content="2048 页面已完成"),
    ]), tools)

    runtime.send(session_id, "生成 2048 HTML 小游戏")
    memories = context.execute(session_id, "memory_list", {"include_candidates": True})[
        "memories"
    ]
    assert len(memories) == 1
    assert memories[0]["status"] == "candidate"
    assert memories[0]["memory_type"] == "episodic"
    assert "index.html" in memories[0]["content"]
    assert "生成 2048 HTML 小游戏" in memories[0]["content"]

    chat_session = store.create_session("普通对话")["session_id"]
    AgentRuntime(
        store, context, SequenceProvider([ModelResponse(content="你好")]), tools
    ).send(chat_session, "你好")
    chat_visible = context.execute(
        chat_session, "memory_list", {"include_candidates": True}
    )["memories"]
    # The file-task candidate is correctly visible across sessions in the same
    # workspace, while ordinary chat created no additional candidate.
    assert [item["memory_id"] for item in chat_visible] == [memories[0]["memory_id"]]


def test_stream_pauses_for_permission_and_resumes_same_tool_call(store, session_id, context):
    class PermissionedRunner:
        schemas = []

        def __init__(self):
            self.broker = PermissionBroker(store)
            self.calls = 0

        def execute(self, current_session_id, name, arguments):
            assert name == "runner_execute"
            self.calls += 1
            self.broker.require(
                current_session_id,
                "runner",
                arguments["profile"],
                "execute",
                {"profile": arguments["profile"], "timeout_seconds": 10, "network": False},
                {"command_template": ["python", "-m", "unittest"], "network": False},
            )
            return {"ok": True, "profile": arguments["profile"], "output": "OK"}

    tools = PermissionedRunner()
    provider = StreamingSequenceProvider([
        [{"type": "done", "response": ModelResponse(
            content="",
            tool_calls=[ToolCall(
                "test_1", "runner_execute", {"profile": "python_unittest"}
            )],
        )}],
        [{"type": "done", "response": ModelResponse(content="测试通过")}],
    ])
    stream = AgentRuntime(store, context, provider, tools).send_stream(
        session_id, "运行 Python 测试"
    )

    before_approval = []
    while True:
        event = next(stream)
        before_approval.append(event)
        if event["type"] == "permission_required":
            break

    permission = before_approval[-1]["request"]
    assert permission["subject_type"] == "runner"
    assert permission["details"]["command_template"] == ["python", "-m", "unittest"]
    assert tools.calls == 1  # first call only performed the permission preflight

    store.decide_permission_request(permission["request_id"], "approved", "once")
    after_approval = list(stream)

    assert tools.calls == 2
    assert {"type": "tool_result", "name": "runner_execute", "ok": True} in after_approval
    assert after_approval[-1]["type"] == "done"
    assert store.get_permission_request(permission["request_id"])["status"] == "consumed"


def test_closing_stream_denies_pending_permission_and_finishes_invocation(
    store, session_id, context
):
    class PermissionedTool:
        schemas = []

        def execute(self, current_session_id, name, arguments):
            PermissionBroker(store).require(
                current_session_id, "runner", "python_pytest", "execute", arguments
            )
            return {"ok": True}

    call = ToolCall(
        "cancel_permission", "runner_execute", {"profile": "python_pytest"}
    )
    stream = AgentRuntime(
        store,
        context,
        StreamingSequenceProvider([[
            {"type": "done", "response": ModelResponse(content="", tool_calls=[call])}
        ]]),
        PermissionedTool(),
    ).send_stream(session_id, "运行 pytest")

    permission = None
    for event in stream:
        if event["type"] == "permission_required":
            permission = event["request"]
            break
    assert permission is not None
    stream.close()

    assert store.get_permission_request(permission["request_id"])["status"] == "denied"
    replay = store.claim_tool_invocation(
        session_id, call.call_id, call.name, call.arguments
    )
    assert replay["action"] == "replay"
    assert replay["result"]["error"] == "用户在审批期间取消了任务"


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


def test_agent_replays_completed_invocation_without_reexecuting_tool(store, session_id, context):
    tools = CountingToolExecutor()
    provider = SequenceProvider([
        ModelResponse(content="", tool_calls=[ToolCall("same_call", "fs_write", {"path": "a.py"})]),
        ModelResponse(content="", tool_calls=[ToolCall("same_call", "fs_write", {"path": "a.py"})]),
        ModelResponse(content="完成"),
    ])
    runtime = AgentRuntime(store, context, provider, tools)

    result = runtime.send(session_id, "写文件")

    assert result.final_event.payload == "完成"
    assert tools.calls == [("fs_write", {"path": "a.py"})]
    tool_results = [event for event in store.list_events(session_id) if event.kind == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[1].payload["replayed"] is True
    assert tool_results[1].payload["original_result_event_id"] == tool_results[0].event_id


def test_agent_blocks_reused_call_id_with_changed_arguments(store, session_id, context):
    tools = CountingToolExecutor()
    provider = SequenceProvider([
        ModelResponse(content="", tool_calls=[ToolCall("same_call", "fs_write", {"path": "a.py"})]),
        ModelResponse(content="", tool_calls=[ToolCall("same_call", "fs_write", {"path": "b.py"})]),
        ModelResponse(content="已处理冲突"),
    ])
    runtime = AgentRuntime(store, context, provider, tools)

    runtime.send(session_id, "写文件")

    assert tools.calls == [("fs_write", {"path": "a.py"})]
    tool_results = [event for event in store.list_events(session_id) if event.kind == "tool_result"]
    assert tool_results[1].payload["invocation_state"] == "conflict"
    assert tool_results[1].payload["ok"] is False


def test_agent_blocks_uncertain_invocation_after_crash_window(store, session_id, context):
    store.claim_tool_invocation(session_id, "crashed_call", "fs_write", {"path": "a.py"})
    tools = CountingToolExecutor()
    provider = SequenceProvider([
        ModelResponse(
            content="", tool_calls=[ToolCall("crashed_call", "fs_write", {"path": "a.py"})]
        ),
        ModelResponse(content="已先检查状态"),
    ])
    runtime = AgentRuntime(store, context, provider, tools)

    runtime.send(session_id, "继续崩溃前任务")

    assert tools.calls == []
    tool_result = next(event for event in store.list_events(session_id) if event.kind == "tool_result")
    assert tool_result.payload["invocation_state"] == "uncertain"
    assert "阻止自动重试" in tool_result.payload["error"]


def test_three_checkpoint_chain_supersedes_old_state_and_finishes(store, session_id, context):
    import pytest

    tools = CountingToolExecutor()
    checkpoints = []
    previous_checkpoint_id = None
    for cycle in range(3):
        provider = SequenceProvider([
            ModelResponse(
                content="",
                tool_calls=[ToolCall(f"run_{cycle}", "fs_write", {"path": f"{cycle}.py"})],
            ),
            ModelResponse(
                content="",
                tool_calls=[ToolCall(f"pending_{cycle}", "fs_read", {"path": f"{cycle}.py"})],
            ),
        ])
        runtime = AgentRuntime(
            store,
            context,
            provider,
            tools,
            AgentRuntimeLimits(max_tool_rounds=1, task_timeout_seconds=10),
        )
        with pytest.raises(AgentLimitError) as caught:
            runtime.send(session_id, "开始长任务" if cycle == 0 else "继续任务")
        checkpoint = caught.value.checkpoint
        checkpoints.append(checkpoint)
        assert store.latest_checkpoint(session_id, active_only=True)["checkpoint_id"] == checkpoint[
            "checkpoint_id"
        ]
        if previous_checkpoint_id:
            assert previous_checkpoint_id in provider.messages[0][0]["content"]
        previous_checkpoint_id = checkpoint["checkpoint_id"]

    finisher_provider = SequenceProvider([ModelResponse(content="长任务已完成")])
    finisher = AgentRuntime(store, context, finisher_provider, tools)
    result = finisher.send(session_id, "继续并完成任务")

    assert result.final_event.payload == "长任务已完成"
    assert checkpoints[-1]["checkpoint_id"] in finisher_provider.messages[0][0]["content"]
    assert store.latest_checkpoint(session_id, active_only=True) is None
    assert len(tools.calls) == 3


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
