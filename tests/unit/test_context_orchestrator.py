import pytest

from zagent.domain.errors import ToolExecutionError


def test_status_reports_budget(context, session_id):
    result = context.execute(session_id, "context_status", {})
    assert result["working_set"]["budget"] > 0


def test_search_validates_arguments(context, session_id):
    with pytest.raises(ToolExecutionError):
        context.execute(session_id, "context_search", {"query": "", "unexpected": True})


def test_pin_and_unpin(context, store, session_id):
    event = store.append_event(session_id, "message", "user", "证据")
    result = context.execute(session_id, "context_pin", {
        "event_ids": [event.event_id], "rationale": "关键"
    })
    assert result["pinned"] == [event.event_id]
    assert result["pinned_tokens"] >= 1
    assert event.event_id in context.build_working_set(session_id).pinned_event_ids
    context.execute(session_id, "context_unpin", {"event_ids": [event.event_id]})
    assert event.event_id not in context.build_working_set(session_id).pinned_event_ids


def test_search_retrieve_and_archive_flow(context, store, session_id):
    event = store.append_event(session_id, "message", "user", "数据库迁移已经完成")
    search = context.execute(session_id, "context_search", {"query": "数据库迁移"})
    assert search["results"][0]["event"]["event_id"] == event.event_id
    retrieved = context.execute(session_id, "context_retrieve", {
        "event_ids": [event.event_id], "max_chars": 1000
    })
    assert retrieved["events"][0]["payload"] == "数据库迁移已经完成"
    archive = context.execute(session_id, "context_archive", {
        "reason": "阶段结束", "start_sequence": 1, "end_sequence": 1,
        "state_update": {
            "goal": "迁移",
            "completed": ["迁移"],
            "decisions": [],
            "risks": [],
            "next_steps": [],
        },
    })
    assert archive["event_range"] == [1, 1]


def test_unknown_tool_is_rejected(context, session_id):
    with pytest.raises(ToolExecutionError):
        context.execute(session_id, "shell", {})
