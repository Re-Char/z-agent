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


def test_working_set_recreates_native_tool_call_message(store, session_id, context):
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [{"call_id": "call_1", "name": "context_status", "arguments": {}}]
    })
    store.append_event(session_id, "tool_result", "tool", {"ok": True}, tool_call_id="call_1")
    messages = context.build_working_set(session_id).messages
    assert messages[-2]["tool_calls"][0]["function"]["name"] == "context_status"
    assert messages[-1]["tool_call_id"] == "call_1"


def test_pinned_event_survives_small_recent_window(store, session_id):
    from zagent.context.orchestrator import ContextOrchestrator
    from zagent.context.working_set import WorkingSetBuilder

    important = store.append_event(session_id, "message", "user", "关键证据")
    store.pin_event(session_id, important.event_id, "必须保留")
    for index in range(10):
        store.append_event(session_id, "message", "assistant", f"普通消息 {index}")
    context = ContextOrchestrator(store, WorkingSetBuilder(store, context_window=4096, recent_event_limit=4))
    assert important.event_id in context.build_working_set(session_id).included_event_ids

