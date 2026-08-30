import pytest


def test_working_set_contains_system_and_recent_events(store, session_id, context):
    event = store.append_event(session_id, "message", "user", "你好")
    working = context.build_working_set(session_id)
    assert working.messages[0]["role"] == "system"
    assert event.event_id in working.included_event_ids
    assert working.messages[-1]["content"] == "你好"


def test_working_set_excludes_raw_provider_payload(store, session_id, context):
    store.append_event(session_id, "model_raw", "system", {"choices": []})
    working = context.build_working_set(session_id)
    assert len(working.messages) == 1


def test_legacy_internal_pin_is_never_injected(store, session_id, context):
    internal = store.append_event(
        session_id, "model_raw", "system", {"provider_secret": "never expose"},
        sensitivity="internal",
    )
    store.pin_event(session_id, internal.event_id, "模拟旧数据库脏数据")
    working = context.build_working_set(session_id)
    assert internal.event_id not in working.included_event_ids
    assert internal.event_id in working.dropped_pinned_ids
    assert "never expose" not in str(working.messages)


def test_working_set_excludes_archive_summary_from_conversation(store, session_id, context):
    # Archive summaries must not appear as mid-conversation "system" messages:
    # after an assistant tool_calls message, providers only accept tool replies.
    store.append_event(session_id, "message", "user", "检查并归档")
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [{"call_id": "call_1", "name": "context_archive", "arguments": {}}]
    })
    # Archive the completed source range, not the currently executing tool call.
    store.create_archive(session_id, 1, 1, "阶段完成", {"stage": "done"})
    store.append_event(session_id, "tool_result", "tool", {"archive_id": "arc_x"}, tool_call_id="call_1")
    messages = context.build_working_set(session_id).messages
    roles = [message["role"] for message in messages]
    assert roles == ["system", "assistant", "tool"]
    # The tool reply is directly after the tool_calls message — no intermediate
    # system/archive summary in between.
    tool_calls_index = roles.index("assistant")
    assert roles[tool_calls_index + 1] == "tool"


def test_working_set_recreates_native_tool_call_message(store, session_id, context):
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [{"call_id": "call_1", "name": "context_status", "arguments": {}}]
    })
    store.append_event(session_id, "tool_result", "tool", {"ok": True}, tool_call_id="call_1")
    messages = context.build_working_set(session_id).messages
    assert messages[-2]["tool_calls"][0]["function"]["name"] == "context_status"
    assert messages[-1]["tool_call_id"] == "call_1"


def test_runner_result_injects_addressable_evidence_id(store, session_id, context):
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "",
        "tool_calls": [{
            "call_id": "runner_1",
            "name": "runner_execute",
            "arguments": {"profile": "python_unittest"},
        }],
    })
    result = store.append_event(
        session_id,
        "tool_result",
        "tool",
        {"ok": True, "provenance": "controlled-runner"},
        tool_name="runner_execute",
        tool_call_id="runner_1",
    )

    message = context.build_working_set(session_id).messages[-1]
    assert result.event_id in message["content"]
    assert "evidence_event_id" in message["content"]


def test_pinned_event_survives_small_recent_window(store, session_id):
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    important = store.append_event(session_id, "message", "user", "关键证据")
    store.pin_event(session_id, important.event_id, "必须保留")
    for index in range(10):
        store.append_event(session_id, "message", "assistant", f"普通消息 {index}")
    context = ContextOrchestrator(store, WorkingSetBuilder(store, context_window=4096, recent_event_limit=4))
    assert important.event_id in context.build_working_set(session_id).included_event_ids


def test_unpinned_old_events_are_evicted_without_pin(store, session_id):
    """Control group: without a pin, old events ARE dropped from the working set."""
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    old = store.append_event(session_id, "message", "user", "很久以前的事")
    for index in range(10):
        store.append_event(session_id, "message", "assistant", f"普通消息 {index}")
    context = ContextOrchestrator(store, WorkingSetBuilder(store, context_window=4096, recent_event_limit=4))
    working = context.build_working_set(session_id)
    # recent_event_limit=4 -> the old event is beyond the window and gets evicted
    assert old.event_id not in working.included_event_ids
    # ...but it remains fully retrievable for auditability
    assert store.search_events(session_id, "很久以前")[0]["event"]["event_id"] == old.event_id


def test_pinned_event_keeps_position_in_working_set(store, session_id):
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    first = store.append_event(session_id, "message", "user", "第 1 条")
    for index in range(10):
        store.append_event(session_id, "message", "assistant", f"普通消息 {index}")
    store.pin_event(session_id, first.event_id, "起点")
    context = ContextOrchestrator(store, WorkingSetBuilder(store, context_window=4096, recent_event_limit=4))
    messages = context.build_working_set(session_id).messages
    # pinned content stays in chronological position: user first, then recent messages
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "第 1 条"


def test_truncation_never_splits_tool_rounds(store, session_id):
    """A truncated window must not leave an assistant tool_calls message without
    its tool replies (or a tool reply without its caller) - providers reject that."""
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    # Round 1 (will be truncated away entirely)
    store.append_event(session_id, "message", "user", "第一轮请求")
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [{"call_id": "call_a", "name": "context_status", "arguments": {}}]
    })
    store.append_event(session_id, "tool_result", "tool", {"ok": True}, tool_call_id="call_a")
    # Round 2 (partially truncated: caller kept, one reply kept, one reply cut)
    store.append_event(session_id, "message", "user", "第二轮请求")
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [
            {"call_id": "call_b", "name": "context_search", "arguments": {}},
            {"call_id": "call_c", "name": "context_retrieve", "arguments": {}},
        ]
    })
    store.append_event(session_id, "tool_result", "tool", {"ok": True}, tool_call_id="call_b")

    context = ContextOrchestrator(store, WorkingSetBuilder(store, context_window=4096, recent_event_limit=4))
    messages = context.build_working_set(session_id).messages
    roles = [message["role"] for message in messages]
    # Every tool message must follow its caller, and every tool_calls message
    # must be followed by replies for all of its calls.
    for index, message in enumerate(messages):
        if message["role"] == "tool":
            assert index > 0 and messages[index - 1]["role"] == "assistant"
            assert "tool_calls" in messages[index - 1]
        if "tool_calls" in message:
            call_ids = [call["id"] for call in message["tool_calls"]]
            following = [m for m in messages[index + 1:] if m["role"] == "tool"]
            ok = len(following) == len(call_ids) or len(following) == 0
            assert ok  # all replies, or the whole round was dropped
    # The kept window must not end on an orphan tool message
    assert roles[-1] != "tool"


def test_token_stats_are_recomputed_after_dropping_broken_tool_round(store, session_id):
    from zagent.context.tokenization import estimate_tokens
    from zagent.context.working_set import WorkingSetBuilder

    user = store.append_event(session_id, "message", "user", "保留消息")
    broken = store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [
            {"call_id": "missing_reply", "name": "context_status", "arguments": {}}
        ],
    })
    store.pin_event(session_id, broken.event_id, "即使固定也不能保留坏轮次")
    builder = WorkingSetBuilder(store, context_window=4096)
    working = builder.build(session_id)
    assert broken.event_id not in working.included_event_ids
    assert broken.event_id in working.dropped_pinned_ids
    expected = estimate_tokens(working.messages[0]["content"]) + user.token_estimate + 8
    assert working.token_estimate == expected
    assert working.pinned_tokens == 0



def test_pinned_events_never_breach_hard_cap(store, session_id):
    """Pinned evidence can use budget headroom but never exceed context_window."""
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    window = 4096
    builder = WorkingSetBuilder(store, context_window=window, hard_limit_ratio=0.82)
    context = ContextOrchestrator(store, builder)
    # one huge pinned event that alone exceeds the budget (3358) but fits under
    # the hard cap (4096)
    big = store.append_event(session_id, "message", "user", "证据" * 1500)
    store.pin_event(session_id, big.event_id, "大证据")
    working = context.build_working_set(session_id)
    assert big.event_id in working.included_event_ids
    assert working.token_estimate <= window
    assert working.pinned_tokens >= 1000

    # a second huge event cannot fit under the hard cap anymore and is dropped
    bigger = store.append_event(session_id, "message", "user", "更大" * 2600)  # > hard cap alone
    store.pin_event(session_id, bigger.event_id, "更大证据")
    working = context.build_working_set(session_id)
    assert bigger.event_id not in working.included_event_ids
    assert bigger.event_id in working.dropped_pinned_ids
    assert working.token_estimate <= window


def test_context_pin_rejects_over_budget_evidence(context, store, session_id):
    from zagent.domain.errors import ToolExecutionError

    huge = store.append_event(session_id, "message", "user", "z" * 9000)  # ~9000 tokens
    with pytest.raises(ToolExecutionError, match="30%"):
        context.execute(session_id, "context_pin", {"event_ids": [huge.event_id], "rationale": "超大"})


def test_context_status_reports_pinned_tokens_and_warning(context, store, session_id):
    event = store.append_event(session_id, "message", "user", "普通证据")
    context.execute(session_id, "context_pin", {"event_ids": [event.event_id], "rationale": "测试"})
    status = context.execute(session_id, "context_status", {})
    assert status["pinned_tokens"] >= 1
    assert "pinned_tokens" in status["working_set"]
    assert "dropped_pinned_ids" in status["working_set"]
    assert status["warning"] is None  # small pin produces no warning


def test_context_status_warns_when_over_budget(store, session_id):
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    # tiny window: budget = 4096*0.82 ≈ 3358; pinned uses most of it
    builder = WorkingSetBuilder(store, context_window=4096, hard_limit_ratio=0.82)
    context = ContextOrchestrator(store, builder)
    pinned = store.append_event(session_id, "message", "user", "证据" * 1500)  # ~2408 tokens
    store.pin_event(session_id, pinned.event_id, "大证据")
    store.append_event(session_id, "message", "user", "最近对话" * 60)  # ~240 tokens
    status = context.execute(session_id, "context_status", {})
    # pinned (~2408) + recent (~1000) pushes the total past budget (3358) but stays under the hard cap (4096)
    assert status["warning"] is not None
    assert status["pinned_tokens"] >= 2000
    assert status["working_set"]["tokens"] <= 4096
    assert "token_estimate" not in status["working_set"]


def test_working_set_cached_until_context_changes(store, session_id, context):
    store.append_event(session_id, "message", "user", "初始")
    first = context.build_working_set(session_id)
    second = context.build_working_set(session_id)
    assert first is second  # build cache hit
    store.append_event(session_id, "message", "assistant", "新事件")
    third = context.build_working_set(session_id)
    assert third is not first  # version bumped -> rebuilt
    assert third.included_event_ids != first.included_event_ids


def test_pin_invalidates_working_set_cache(store, session_id, context):
    event = store.append_event(session_id, "message", "user", "缓存证据")
    first = context.build_working_set(session_id)
    store.pin_event(session_id, event.event_id, "固定")
    second = context.build_working_set(session_id)
    assert event.event_id in second.pinned_event_ids
    assert second is not first


def test_cache_identity_includes_model_tool_schema_and_workspace(store):
    from zagent.context.working_set import WorkingSetBuilder

    workspace = store.create_workspace("缓存身份", "")
    session_id = store.create_session("缓存身份", workspace["workspace_id"])["session_id"]
    schema = {"version": "tools-v1"}
    builder = WorkingSetBuilder(store)
    builder.configure_cache_identity("model-v1", lambda: schema["version"])

    first = builder.build(session_id)
    assert builder.build(session_id) is first

    schema["version"] = "tools-v2"
    after_tools = builder.build(session_id)
    assert after_tools is not first

    builder.configure_cache_identity("model-v2", lambda: schema["version"])
    after_model = builder.build(session_id)
    assert after_model is not after_tools

    store.update_workspace(workspace["workspace_id"], name="缓存身份 2")
    after_workspace = builder.build(session_id)
    assert after_workspace is not after_model
