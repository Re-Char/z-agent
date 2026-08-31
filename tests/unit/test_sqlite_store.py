import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from zagent.domain.errors import ConcurrentUpdateError, NotFoundError, ValidationError


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


def test_first_user_message_replaces_only_the_default_session_title(store):
    session = store.create_session("新任务")
    session_id = session["session_id"]
    content = (
        "  请检查   当前项目的 MCP 工具调用，并给出一个不会过度设计的最小实现方案，"
        "标题需要保持单行且不能无限增长。  "
    )

    store.append_event(session_id, "message", "user", content)
    title = store.get_session(session_id)["title"]
    assert title == (
        "请检查 当前项目的 MCP 工具调用，并给出一个不会过度设计的最小实现方案，"
        "标题需要保持单行且…"
    )
    assert len(title) == 48

    store.append_event(session_id, "message", "user", "第二条消息不能覆盖标题")
    assert store.get_session(session_id)["title"] == title

    named = store.create_session("用户指定标题")
    store.append_event(named["session_id"], "message", "user", "不会覆盖")
    assert store.get_session(named["session_id"])["title"] == "用户指定标题"


def test_context_version_persists_and_is_visible_across_store_instances(tmp_path):
    from zagent.storage.sqlite_store import SqliteStore

    data_dir = tmp_path / "data"
    writer = SqliteStore(str(data_dir))
    reader = None
    reopened = None
    try:
        session_id = writer.create_session("持久化版本")["session_id"]
        reader = SqliteStore(str(data_dir))
        assert writer.context_version(session_id) == 0
        assert reader.context_version(session_id) == 0

        writer.append_event(session_id, "message", "user", "跨进程失效")
        assert writer.context_version(session_id) == 1
        assert reader.context_version(session_id) == 1

        writer.close()
        writer = None
        reopened = SqliteStore(str(data_dir))
        assert reopened.context_version(session_id) == 1
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        if reopened is not None:
            reopened.close()


def test_shared_connection_serializes_parallel_renderer_reads(store, session_id):
    for index in range(8):
        store.append_event(session_id, "message", "user", f"并发事件 {index}")
    barrier = Barrier(12)

    def read_repeatedly(worker: int) -> int:
        barrier.wait()
        for iteration in range(60):
            selector = (worker + iteration) % 4
            if selector == 0:
                assert store.get_session(session_id)["event_count"] == 8
            elif selector == 1:
                assert len(store.list_events(session_id)) == 8
            elif selector == 2:
                assert store.context_version(session_id) == 8
            else:
                assert len(store.recent_active_events(session_id, 4)) == 4
        return worker

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sorted(pool.map(read_repeatedly, range(12))) == list(range(12))


def test_tool_invocation_claim_replays_and_blocks_conflicts(store, session_id):
    first = store.claim_tool_invocation(session_id, "call_1", "fs_write", {"path": "a.py"})
    assert first["action"] == "execute"
    result_event = store.complete_tool_invocation(
        session_id, "call_1", "fs_write", {"ok": True, "sha256": "a" * 64}
    )

    replay = store.claim_tool_invocation(session_id, "call_1", "fs_write", {"path": "a.py"})
    assert replay["action"] == "replay"
    assert replay["result_event_id"] == result_event.event_id
    assert replay["result"]["sha256"] == "a" * 64

    conflict = store.claim_tool_invocation(session_id, "call_1", "fs_write", {"path": "b.py"})
    assert conflict["action"] == "conflict"

    running = store.claim_tool_invocation(session_id, "call_crashed", "fs_write", {"path": "c.py"})
    assert running["action"] == "execute"
    uncertain = store.claim_tool_invocation(
        session_id, "call_crashed", "fs_write", {"path": "c.py"}
    )
    assert uncertain["action"] == "uncertain"


def test_tool_invocation_claim_is_unique_across_store_instances(tmp_path):
    from zagent.storage.sqlite_store import SqliteStore

    data_dir = tmp_path / "data"
    first = SqliteStore(str(data_dir))
    second = SqliteStore(str(data_dir))
    try:
        session_id = first.create_session("跨实例 invocation")["session_id"]
        barrier = Barrier(2)

        def claim(store):
            barrier.wait()
            return store.claim_tool_invocation(
                session_id, "shared_call", "fs_write", {"path": "shared.py"}
            )["action"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            actions = sorted(pool.map(claim, [first, second]))
        assert actions == ["execute", "uncertain"]
    finally:
        first.close()
        second.close()


def test_cross_process_optimistic_event_write_allows_only_one_stale_revision(tmp_path):
    from zagent.storage.sqlite_store import SqliteStore

    data_dir = tmp_path / "data"
    first = SqliteStore(str(data_dir))
    second = SqliteStore(str(data_dir))
    try:
        session_id = first.create_session("跨进程 CAS")["session_id"]
        expected = first.context_version(session_id)
        barrier = Barrier(2)

        def append(store, content):
            barrier.wait()
            try:
                store.append_event(
                    session_id,
                    "message",
                    "user",
                    content,
                    expected_context_version=expected,
                )
                return "written"
            except ConcurrentUpdateError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(append, [first, second], ["一", "二"]))

        assert outcomes == ["stale", "written"]
        assert first.context_version(session_id) == expected + 1
        assert len(first.list_events(session_id)) == 1
    finally:
        first.close()
        second.close()


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


def test_checkpoint_is_atomic_addressable_and_persistent(store, session_id):
    trigger = store.append_event(session_id, "message", "user", "完成长任务")
    before = store.context_version(session_id)
    checkpoint = store.create_checkpoint(
        session_id,
        trigger.event_id,
        "max_tool_rounds",
        {
            "schema_version": 1,
            "status": "paused",
            "completed": [{"tool_result_event_id": "evt_evidence"}],
            "pending": [{"tool": "fs_read"}],
        },
    )

    assert checkpoint["checkpoint_id"].startswith("chk_")
    assert store.context_version(session_id) == before + 1
    event = store.get_event(checkpoint["checkpoint_event_id"])
    assert event.kind == "checkpoint"
    assert event.parent_event_id == trigger.event_id
    assert event.payload["state"]["status"] == "paused"
    assert store.latest_checkpoint(session_id)["state"]["pending"][0]["tool"] == "fs_read"


def test_archive_rejects_fully_archived_range(store, session_id):
    store.append_event(session_id, "message", "user", "开始")
    store.create_archive(session_id, 1, 1, "完成", {"stage": "done"})
    with pytest.raises(ValidationError, match="already archived"):
        store.create_archive(session_id, 1, 1, "重复归档", {"stage": "done"})


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
        assert store.context_version("ses_old") == 0
        store.append_event("ses_old", "message", "user", "迁移后仍可失效缓存")
        assert store.context_version("ses_old") == 1
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
