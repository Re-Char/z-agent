import hashlib

import pytest

from zagent.domain.errors import NotFoundError, ValidationError


def test_create_and_list_session(store):
    created = store.create_session("研究任务")
    assert created["title"] == "研究任务"
    assert store.list_sessions()[0]["session_id"] == created["session_id"]


def test_append_event_preserves_hash_and_sequence(store, session_id):
    first = store.append_event(session_id, "message", "user", "第一条")
    second = store.append_event(session_id, "message", "assistant", "第二条")
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.payload_sha256 == hashlib.sha256("第一条".encode()).hexdigest()
    assert store.get_event(first.event_id).payload == "第一条"


def test_large_payload_round_trips_through_blob(store, session_id):
    payload = "中文大输出" * 100
    event = store.append_event(session_id, "tool_result", "tool", payload)
    assert store.get_event(event.event_id).payload == payload


def test_chinese_search_finds_event(store, session_id):
    event = store.append_event(session_id, "message", "user", "请检查数据库迁移是否成功")
    results = store.search_events(session_id, "数据库迁移")
    assert results[0]["event"]["event_id"] == event.event_id


def test_pin_rejects_cross_session_event(store, session_id):
    other = store.create_session("另一个")["session_id"]
    event = store.append_event(other, "message", "user", "证据")
    with pytest.raises(ValidationError):
        store.pin_event(session_id, event.event_id, "错误固定")


def test_archive_preserves_source_ids(store, session_id):
    first = store.append_event(session_id, "message", "user", "开始")
    second = store.append_event(session_id, "message", "assistant", "完成")
    archive = store.create_archive(session_id, 1, 2, "阶段完成", {
        "goal": "测试", "completed": ["完成"], "decisions": [], "risks": [], "next_steps": []
    })
    summary = store.get_event(archive["summary_event_id"])
    assert summary.payload["source_event_ids"] == [first.event_id, second.event_id]
    assert store.latest_archive(session_id)["state"]["goal"] == "测试"


def test_unknown_session_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.list_events("ses_missing")

