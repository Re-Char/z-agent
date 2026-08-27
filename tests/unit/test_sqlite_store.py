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



def test_workspace_creation_and_session_scoping(tmp_path):
    from zagent.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "data"))
    try:
        workspaces = store.list_workspaces()
        assert len(workspaces) == 1  # default workspace auto-created
        default_id = workspaces[0]["workspace_id"]

        project = store.create_workspace("项目 A", "/tmp/project-a")
        assert project["name"] == "项目 A"
        assert project["path"] == "/tmp/project-a"
        assert project["session_count"] == 0

        session = store.create_session("项目会话", project["workspace_id"])
        assert session["workspace_id"] == project["workspace_id"]
        store.create_session("默认会话")

        assert len(store.list_sessions(project["workspace_id"])) == 1
        assert [s["title"] for s in store.list_sessions(default_id)] == ["默认会话"]
        assert len(store.list_sessions()) == 2

        listed = store.list_workspaces()
        by_name = {item["name"]: item for item in listed}
        assert by_name["项目 A"]["session_count"] == 1
    finally:
        store.close()


def test_v1_database_migrates_to_workspace_schema(tmp_path):
    import sqlite3

    db_path = tmp_path / "data" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO sessions VALUES('ses_old','旧会话','t','t')")
    conn.commit()
    conn.close()

    from zagent.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "data"))
    try:
        assert store.get_session("ses_old")["session_id"] == "ses_old"
        assert len(store.list_workspaces()) == 1
        assert store.list_sessions()[0]["title"] == "旧会话"
    finally:
        store.close()


def test_workspace_path_update(tmp_path):
    from zagent.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "data"))
    try:
        workspace = store.create_workspace("项目", "")
        assert workspace["path"] == ""
        updated = store.update_workspace(workspace["workspace_id"], path="/tmp/project-a")
        assert updated["path"] == "/tmp/project-a"
        renamed = store.update_workspace(workspace["workspace_id"], name="项目 A")
        assert renamed["name"] == "项目 A"
        assert renamed["path"] == "/tmp/project-a"
    finally:
        store.close()


def test_archive_failure_leaves_no_orphan_event(tmp_path):
    import sqlite3

    from zagent.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "data"))
    try:
        session = store.create_session("原子性")
        session_id = session["session_id"]
        store.append_event(session_id, "message", "user", "内容")
        store._db.execute("DROP TABLE archives")  # force the second write to fail
        with pytest.raises(sqlite3.OperationalError):
            store.create_archive(session_id, 1, 1, "收尾", {"stage": "done"})
        events = store.list_events(session_id)
        assert all(event.kind != "archive" for event in events)  # no orphan summary event
    finally:
        store.close()


def test_search_ranks_exact_phrase_first(store, session_id):
    store.append_event(session_id, "message", "user", "数据库迁移计划已经完成")
    store.append_event(session_id, "message", "user", "今天讨论数据库和缓存")
    results = store.search_events(session_id, "数据库迁移")
    assert results[0]["event"]["payload"] == "数据库迁移计划已经完成"
    assert results[0]["score"] < results[1]["score"]
