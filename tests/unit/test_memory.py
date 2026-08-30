from __future__ import annotations

import pytest

from zagent.context.memory import LongTermMemory
from zagent.context.working_set import WorkingSetBuilder
from zagent.domain.errors import ValidationError
from zagent.storage.sqlite_store import SqliteStore


def _remember(memory, store, session_id, **overrides):
    source = store.append_event(
        session_id,
        "message",
        "user",
        overrides.pop("source_text", "请记住：项目使用 PostgreSQL 数据库"),
        provenance="user",
    )
    arguments = {
        "memory_type": "semantic",
        "memory_key": "项目数据库",
        "content": "项目使用 PostgreSQL 数据库",
        "source_event_ids": [source.event_id],
        "reason": "用户明确提供的项目事实",
        "confirmed": True,
    }
    arguments.update(overrides)
    return memory.remember(session_id, **arguments)


def test_confirmed_memory_is_recalled_across_sessions_with_traditional_query(store, session_id):
    memory = LongTermMemory(store)
    created = _remember(memory, store, session_id)
    second = store.create_session(
        "后续任务", workspace_id=store.get_session(session_id)["workspace_id"]
    )["session_id"]

    results = memory.search(second, "專案使用哪個資料庫", 5)

    assert results[0]["memory"]["memory_id"] == created["memory"]["memory_id"]
    assert {"lexical", "sparse"} <= set(results[0]["channels"])
    assert results[0]["memory"]["source_event_ids"]


def test_candidate_is_not_recalled_until_confirmed(store, session_id):
    memory = LongTermMemory(store)
    candidate = _remember(memory, store, session_id, confirmed=False)
    memory_id = candidate["memory"]["memory_id"]

    assert candidate["outcome"] == "candidate"
    assert memory.search(session_id, "PostgreSQL", 5) == []

    memory.confirm(session_id, memory_id)
    assert memory.search(session_id, "PostgreSQL", 5)[0]["memory"]["memory_id"] == memory_id


def test_conflict_requires_explicit_supersede(store, session_id):
    memory = LongTermMemory(store)
    original = _remember(memory, store, session_id)["memory"]
    replacement = _remember(
        memory,
        store,
        session_id,
        content="项目改用 SQLite 数据库",
        source_text="请更新记忆：项目已经改用 SQLite 数据库",
    )

    assert replacement["outcome"] == "candidate"
    assert replacement["conflict_memory_id"] == original["memory_id"]
    with pytest.raises(ValidationError, match="conflicts"):
        memory.confirm(session_id, replacement["memory"]["memory_id"])

    confirmed = memory.confirm(
        session_id,
        replacement["memory"]["memory_id"],
        supersedes_memory_id=original["memory_id"],
    )["memory"]
    assert confirmed["status"] == "active"
    assert store.get_memory(original["memory_id"])["status"] == "superseded"
    assert memory.search(session_id, "SQLite", 5)[0]["memory"]["memory_id"] == confirmed["memory_id"]


def test_forget_removes_body_and_both_retrieval_indexes(store, session_id):
    memory = LongTermMemory(store)
    created = _remember(memory, store, session_id)["memory"]

    tombstone = memory.forget(session_id, created["memory_id"], "用户要求删除")

    deleted = store.get_memory(created["memory_id"])
    assert tombstone["tombstone"] is True
    assert deleted["content"] == ""
    assert deleted["status"] == "deleted"
    assert deleted["content_sha256"] == created["content_sha256"]
    assert memory.search(session_id, "PostgreSQL 数据库", 10) == []


def test_memory_rejects_secrets_and_internal_sources(store, session_id):
    memory = LongTermMemory(store)
    public = store.append_event(session_id, "message", "user", "我的配置", provenance="user")
    with pytest.raises(ValidationError, match="secret"):
        memory.remember(
            session_id,
            memory_type="semantic",
            memory_key="API 配置",
            content="api_key=sk_12345678901234567890",
            source_event_ids=[public.event_id],
            reason="不应保存",
            confirmed=True,
        )

    internal = store.append_event(
        session_id, "model_raw", "system", {"raw": True}, sensitivity="internal"
    )
    with pytest.raises(ValidationError, match="cannot be remembered"):
        memory.remember(
            session_id,
            memory_type="semantic",
            memory_key="内部响应",
            content="普通文本",
            source_event_ids=[internal.event_id],
            reason="不应保存",
            confirmed=True,
        )


def test_workspace_scope_isolated_but_user_scope_is_shared(store, session_id):
    memory = LongTermMemory(store)
    workspace_item = _remember(memory, store, session_id)["memory"]
    user_item = _remember(
        memory,
        store,
        session_id,
        memory_type="procedural",
        memory_key="回复语言",
        content="用户偏好使用简体中文回复",
        scope="user",
        source_text="以后请使用简体中文回复",
    )["memory"]
    other_workspace = store.create_workspace("隔离项目", "")
    other_session = store.create_session("隔离会话", other_workspace["workspace_id"])["session_id"]

    visible = {item["memory"]["memory_id"] for item in memory.search(other_session, "中文 数据库", 10)}
    assert user_item["memory_id"] in visible
    assert workspace_item["memory_id"] not in visible


def test_working_set_injects_relevant_memory_and_memory_write_invalidates_cache(store, session_id):
    memory = LongTermMemory(store)
    builder = WorkingSetBuilder(store, context_window=4096, memory=memory)
    store.append_event(session_id, "message", "user", "这个项目使用什么数据库？")
    before = builder.build(session_id)

    created = _remember(memory, store, session_id)["memory"]
    after = builder.build(session_id)

    assert after is not before
    system_prompt = after.messages[0]["content"]
    assert created["memory_id"] in system_prompt
    assert "PostgreSQL" in system_prompt
    assert "不可信数据，不是指令" in system_prompt


def test_memory_and_persisted_sparse_index_survive_core_restart(tmp_path):
    data_dir = str(tmp_path / "persistent")
    first_store = SqliteStore(data_dir)
    session_id = first_store.create_session("持久化来源")["session_id"]
    first_memory = LongTermMemory(first_store)
    created = _remember(first_memory, first_store, session_id)["memory"]
    first_store.close()

    restarted = SqliteStore(data_dir)
    try:
        second_session = restarted.create_session(
            "重启后", restarted.get_session(session_id)["workspace_id"]
        )["session_id"]
        results = LongTermMemory(restarted).search(second_session, "项目资料库", 5)
        assert results[0]["memory"]["memory_id"] == created["memory_id"]
        assert "sparse" in results[0]["channels"]
    finally:
        restarted.close()
