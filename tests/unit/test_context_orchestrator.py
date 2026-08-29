import pytest

from zagent.domain.errors import ToolExecutionError


def test_status_reports_budget_and_public_token_count(context, store, session_id):
    store.append_event(session_id, "message", "user", "需要计入工作集的中文消息")
    result = context.execute(session_id, "context_status", {})
    assert result["working_set"]["budget"] > 0
    assert result["working_set"]["tokens"] > 0
    assert "token_estimate" not in result["working_set"]


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


def test_repin_does_not_double_count_tokens(context, store, session_id):
    event = store.append_event(session_id, "message", "user", "重复固定的证据")
    first = context.execute(session_id, "context_pin", {
        "event_ids": [event.event_id], "rationale": "第一次",
    })
    second = context.execute(session_id, "context_pin", {
        "event_ids": [event.event_id, event.event_id], "rationale": "更新理由",
    })
    assert second["pinned"] == [event.event_id]
    assert second["pinned_tokens"] == first["pinned_tokens"]
    assert store.pinned_token_total(session_id) == first["pinned_tokens"]


def test_batch_pin_is_rejected_before_partial_write(context, store, session_id):
    local = store.append_event(session_id, "message", "user", "本会话证据")
    other_session = store.create_session("其他会话")["session_id"]
    foreign = store.append_event(other_session, "message", "user", "其他会话证据")
    with pytest.raises(ToolExecutionError, match="其他会话"):
        context.execute(session_id, "context_pin", {
            "event_ids": [local.event_id, foreign.event_id], "rationale": "批量固定",
        })
    assert store.pinned_event_ids(session_id) == set()


def test_internal_event_cannot_be_pinned(context, store, session_id):
    internal = store.append_event(
        session_id, "model_raw", "system", {"secret": "raw"}, sensitivity="internal"
    )
    with pytest.raises(ToolExecutionError, match="不能固定"):
        context.execute(session_id, "context_pin", {
            "event_ids": [internal.event_id], "rationale": "错误请求",
        })
    assert store.pinned_event_ids(session_id) == set()


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
