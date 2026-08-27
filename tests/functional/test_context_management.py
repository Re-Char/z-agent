"""Context management audit: how archives, eviction, retrieval and pinning interact.

Verifies the claims behind the inspector UI:
- archive creates a summary + state that is injected into the system prompt;
- when the recent window overflows, old unpinned events are evicted from the
  working set but stay searchable/retrievable (auditability);
- pinned events survive eviction and archives;
- the model conversation is rebuilt from the same working set every turn.
"""

import json

from zagent.bootstrap import ApplicationContainer
from zagent.context.orchestrator import ContextOrchestrator
from zagent.context.working_set import WorkingSetBuilder


def test_archive_summary_and_state_reach_system_prompt(store, session_id):
    for index in range(5):
        store.append_event(session_id, "message", "user", f"问题 {index}")
        store.append_event(session_id, "message", "assistant", f"回答 {index}")
    archive = store.create_archive(session_id, 1, 4, "阶段一完成", {"stage": "planning"})
    assert archive["event_range"] == [1, 4]
    archive_event = next(event for event in store.list_events(session_id) if event.kind == "archive")
    assert len(archive_event.payload["source_event_ids"]) == 4
    working = WorkingSetBuilder(store, recent_event_limit=24).build(session_id)
    system_prompt = working.messages[0]["content"]
    assert "当前任务状态" in system_prompt
    assert "planning" in system_prompt
    assert archive["archive_id"] in system_prompt


def test_archive_event_never_breaks_tool_sequence(store, session_id):
    """Archive summaries must not land between assistant tool_calls and tool replies."""
    store.append_event(session_id, "message", "user", "开始")
    store.append_event(session_id, "assistant_tool_calls", "assistant", {
        "content": "", "tool_calls": [{"call_id": "call_1", "name": "context_archive", "arguments": {}}]
    })
    store.create_archive(session_id, 1, 1, "摘要", {"done": True})
    store.append_event(session_id, "tool_result", "tool", {"ok": True}, tool_call_id="call_1")
    roles = [m["role"] for m in WorkingSetBuilder(store).build(session_id).messages]
    assert roles[-2] == "assistant"
    assert roles[-1] == "tool"


def test_evicted_events_stay_retrievable_after_archive(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        session = container.store.create_session("长会话审计")
        session_id = session["session_id"]
        first = container.store.append_event(session_id, "message", "user", "最早的需求说明")
        # enough traffic to overflow the small recent window (limit=6)
        for index in range(30):
            container.store.append_event(session_id, "message", "assistant", f"过程 {index}")
        # build a tight working set (limit 6) and pin the first event
        context = ContextOrchestrator(
            container.store,
            WorkingSetBuilder(container.store, context_window=4096, recent_event_limit=6),
        )
        working = context.build_working_set(session_id)
        assert first.event_id not in working.included_event_ids  # evicted

        container.store.pin_event(session_id, first.event_id, "核心需求")
        working = context.build_working_set(session_id)
        assert first.event_id in working.included_event_ids  # pinned -> survives

        # archiving the traffic does not remove the pinned event either
        container.store.create_archive(session_id, 1, 30, "压缩过程", {"stage": "done"})
        working = context.build_working_set(session_id)
        assert first.event_id in working.included_event_ids

        # original content is still fully retrievable
        found = container.store.search_events(session_id, "最早的需求说明")
        assert found and found[0]["event"]["event_id"] == first.event_id
        retrieved = container.context.execute(session_id, "context_retrieve", {
            "event_ids": [first.event_id], "max_chars": 256,
        })
        assert "最早的需求说明" in retrieved["events"][0]["payload"]
    finally:
        container.close()


def test_full_agent_loop_with_archive_and_pin(tmp_path):
    """End-to-end: echo agent + archive + pin, working set stays valid every turn."""
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        session = container.store.create_session("归档流程")
        session_id = session["session_id"]
        container.agent.send(session_id, "第一步：介绍项目背景")
        container.agent.send(session_id, "第二步：继续讨论")
        events = container.store.list_events(session_id)
        archive = container.context.execute(session_id, "context_archive", {
            "start_sequence": 1, "end_sequence": events[-1].sequence, "reason": "阶段收尾",
            "state_update": {
                "goal": "验证归档", "completed": ["讨论背景"], "decisions": [],
                "risks": [], "next_steps": ["总结"],
            },
        })
        assert archive["archive_id"]
        # after archiving, the next model turn sees the archive state in its prompt
        result = container.agent.send(session_id, "第三步：总结归档内容")
        working = container.context.build_working_set(session_id)
        system_prompt = working.messages[0]["content"]
        assert "当前任务状态" in system_prompt
        assert json.dumps(working.to_dict(), ensure_ascii=False)
        assert result.final_event.payload  # echo replies regardless
        # every message role sequence is provider-valid
        roles = [m["role"] for m in working.messages]
        assert roles[0] == "system"
        assert set(roles) <= {"system", "user", "assistant", "tool"}
    finally:
        container.close()


def test_working_set_requests_are_byte_stable_without_changes(store, session_id):
    """Cache-friendly property: identical state must produce byte-identical prompts.

    DeepSeek caches the request prefix; any incidental change (ordering, whitespace,
    re-serialization) invalidates the prefix cache and lowers the hit rate.
    """
    for index in range(3):
        store.append_event(session_id, "message", "user", f"问题 {index}")
        store.append_event(session_id, "message", "assistant", f"回答 {index}")
    builder = WorkingSetBuilder(store, recent_event_limit=24)
    first = json.dumps(builder.build(session_id).messages, ensure_ascii=False)
    second = json.dumps(builder.build(session_id).messages, ensure_ascii=False)
    assert first == second


def test_archive_injection_does_not_churn_system_prompt(store, session_id):
    """System prompt must only change when the archive state actually changes."""
    store.append_event(session_id, "message", "user", "开工")
    builder = WorkingSetBuilder(store, recent_event_limit=24)
    before = builder.build(session_id).messages[0]["content"]
    store.create_archive(session_id, 1, 1, "收尾", {"stage": "done"})
    after = builder.build(session_id).messages[0]["content"]
    assert before != after  # archive state is injected
    # a second build with no new archive is byte-stable again
    assert builder.build(session_id).messages[0]["content"] == after
