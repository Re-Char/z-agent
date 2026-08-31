from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from zagent.context.tokenization import estimate_tokens, excerpt, normalize_text, searchable_text
from zagent.domain.errors import ConcurrentUpdateError, NotFoundError, ValidationError
from zagent.domain.models import EventRecord

from .blob_store import BlobStore
from .schema import MIGRATIONS_SQL, SCHEMA_SQL

DEFAULT_SESSION_TITLES = {"新任务", "New task", "New Task"}
SESSION_TITLE_MAX_CHARS = 48


def session_title_from_message(content: str) -> str:
    """Build a compact, single-line title from the first user message."""
    compact = " ".join(content.split()).strip()
    if not compact:
        return "新任务"
    if len(compact) <= SESSION_TITLE_MAX_CHARS:
        return compact
    return compact[: SESSION_TITLE_MAX_CHARS - 1].rstrip() + "…"


def _serialized(method):
    """Serialize every operation sharing the process-wide SQLite connection."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class SqliteStore:
    """Append-only session/event repository with addressable external blobs."""

    def __init__(self, data_dir: str, blob_threshold: int = 32_768) -> None:
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self._blob_threshold = blob_threshold
        self._blobs = BlobStore(root / "blobs")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(root / "state.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._db:
            self._db.executescript(SCHEMA_SQL)
            for statement in MIGRATIONS_SQL:
                # Column already present on fresh databases created with v2 schema.
                with contextlib.suppress(sqlite3.OperationalError):
                    self._db.execute(statement)
            self._ensure_search_index_version()
            self._ensure_memory_search_index_version()
        self._ensure_default_workspace()

    def _ensure_search_index_version(self) -> None:
        """Rebuild FTS when Chinese normalization/token rules change."""
        index_version = 2
        row = self._db.execute("SELECT value FROM metadata WHERE key='event_index_version'").fetchone()
        if row is not None and int(row["value"]) == index_version:
            return
        self._db.execute("DELETE FROM event_fts")
        rows = self._db.execute("SELECT * FROM events ORDER BY session_id,sequence").fetchall()
        self._db.executemany(
            "INSERT INTO event_fts(event_id,session_id,search_text) VALUES(?,?,?)",
            [
                (
                    item["event_id"],
                    item["session_id"],
                    searchable_text(self._serialize(self._deserialize_payload(item))),
                )
                for item in rows
            ],
        )
        self._db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('event_index_version',?)",
            (index_version,),
        )

    def _ensure_memory_search_index_version(self) -> None:
        """Rebuild persisted sparse terms when deterministic weights change."""
        index_version = 2
        row = self._db.execute("SELECT value FROM metadata WHERE key='memory_index_version'").fetchone()
        if row is not None and int(row["value"]) == index_version:
            return
        # Local import avoids a module-level cycle: retrieval's event search
        # depends on SqliteStore, while this migration only needs sparse_terms.
        from zagent.context.retrieval import sparse_terms

        self._db.execute("DELETE FROM memory_terms")
        rows = self._db.execute(
            """SELECT memory_id,memory_key,content FROM memories
               WHERE status IN ('active','candidate') AND content<>''"""
        ).fetchall()
        for item in rows:
            terms = sparse_terms(f"{item['memory_key']} {item['content']}")
            self._db.executemany(
                "INSERT INTO memory_terms(memory_id,term,weight) VALUES(?,?,?)",
                [(item["memory_id"], term, weight) for term, weight in terms.items()],
            )
        self._db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('memory_index_version',?)",
            (index_version,),
        )

    def _ensure_default_workspace(self) -> None:
        row = self._db.execute("SELECT workspace_id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            self.create_workspace("默认工作区", "")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _serialize(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # --- workspaces ----------------------------------------------------------

    @_serialized
    def create_workspace(self, name: str, path: str = "") -> Dict[str, Any]:
        workspace_id = "ws_" + uuid.uuid4().hex
        now = self._now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO workspaces(workspace_id,name,path,version,created_at) VALUES(?,?,?,?,?)",
                (workspace_id, name.strip() or "未命名工作区", path.strip(), 0, now),
            )
        return self.get_workspace(workspace_id)

    @_serialized
    def get_workspace(self, workspace_id: str) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        if row is None:
            raise NotFoundError("workspace not found")
        result = dict(row)
        result["session_count"] = self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE workspace_id=?", (workspace_id,)
        ).fetchone()[0]
        return result

    @_serialized
    def update_workspace(
        self, workspace_id: str, name: Optional[str] = None, path: Optional[str] = None
    ) -> Dict[str, Any]:
        self.get_workspace(workspace_id)
        with self._lock, self._db:
            if name is not None:
                clean_name = name.strip() or "未命名工作区"
                self._db.execute(
                    "UPDATE workspaces SET name=? WHERE workspace_id=?", (clean_name, workspace_id)
                )
            if path is not None:
                self._db.execute(
                    "UPDATE workspaces SET path=? WHERE workspace_id=?", (path.strip(), workspace_id)
                )
                session_rows = self._db.execute(
                    "SELECT session_id FROM sessions WHERE workspace_id=?", (workspace_id,)
                ).fetchall()
                for row in session_rows:
                    self._bump_context_version(row["session_id"])
            if name is not None or path is not None:
                self._db.execute(
                    "UPDATE workspaces SET version=version+1 WHERE workspace_id=?",
                    (workspace_id,),
                )
        return self.get_workspace(workspace_id)

    @_serialized
    def list_workspaces(self) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            """SELECT w.*, COUNT(s.session_id) AS session_count
               FROM workspaces w LEFT JOIN sessions s ON s.workspace_id=w.workspace_id
               GROUP BY w.workspace_id ORDER BY w.created_at"""
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def default_workspace_id(self) -> str:
        row = self._db.execute("SELECT workspace_id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()
        return row["workspace_id"] if row else self.create_workspace("默认工作区", "")["workspace_id"]

    @_serialized
    def create_session(self, title: str = "新任务", workspace_id: Optional[str] = None) -> Dict[str, Any]:
        session_id = "ses_" + uuid.uuid4().hex
        now = self._now()
        workspace = workspace_id or self.default_workspace_id()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions(session_id,title,workspace_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (session_id, title.strip() or "新任务", workspace, now, now),
            )
        return self.get_session(session_id)

    @_serialized
    def get_session(self, session_id: str) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("session not found")
        result = dict(row)
        result["event_count"] = self._db.execute(
            "SELECT COUNT(*) FROM events WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        return result

    @_serialized
    def list_sessions(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if workspace_id:
            self.get_workspace(workspace_id)
            rows = self._db.execute(
                """SELECT s.*, COUNT(e.event_id) AS event_count
                   FROM sessions s LEFT JOIN events e ON e.session_id=s.session_id
                   WHERE s.workspace_id=? GROUP BY s.session_id ORDER BY s.updated_at DESC""",
                (workspace_id,),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT s.*, COUNT(e.event_id) AS event_count
                   FROM sessions s LEFT JOIN events e ON e.session_id=s.session_id
                   GROUP BY s.session_id ORDER BY s.updated_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def append_event(
        self,
        session_id: str,
        kind: str,
        role: str,
        payload: Any,
        *,
        parent_event_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        sensitivity: str = "normal",
        provenance: str = "runtime",
        expected_context_version: Optional[int] = None,
    ) -> EventRecord:
        with self._lock, self._db:
            if expected_context_version is not None:
                self._claim_context_version(session_id, expected_context_version)
            event = self._insert_event(
                session_id,
                kind,
                role,
                payload,
                parent_event_id=parent_event_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tags=tags,
                sensitivity=sensitivity,
                provenance=provenance,
            )
            if expected_context_version is None:
                self._bump_context_version(session_id)
        return event

    def _insert_event(
        self,
        session_id: str,
        kind: str,
        role: str,
        payload: Any,
        *,
        parent_event_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        sensitivity: str = "normal",
        provenance: str = "runtime",
    ) -> EventRecord:
        """Insert one event row (events + fts + session touch) within the caller's transaction."""
        serialized = self._serialize(payload)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        inline, reference = self._store_payload(serialized)
        event_id = "evt_" + uuid.uuid4().hex
        now = self._now()
        self.get_session(session_id)
        sequence = self._db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        self._db.execute(
            """INSERT INTO events(event_id,session_id,sequence,timestamp,kind,role,payload_inline,
               payload_ref,payload_sha256,token_estimate,parent_event_id,tool_name,tool_call_id,tags,
               sensitivity,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                session_id,
                sequence,
                now,
                kind,
                role,
                inline,
                reference,
                digest,
                estimate_tokens(serialized),
                parent_event_id,
                tool_name,
                tool_call_id,
                json.dumps(list(tags or []), ensure_ascii=False),
                sensitivity,
                provenance,
            ),
        )
        self._db.execute(
            "INSERT INTO event_fts(event_id,session_id,search_text) VALUES(?,?,?)",
            (event_id, session_id, searchable_text(serialized)),
        )
        if kind == "message" and role == "user" and isinstance(payload, str):
            # A manually named session keeps its title. Only replace the generic
            # placeholder, and only for the first user message in the session.
            user_messages = self._db.execute(
                """SELECT COUNT(*) FROM events
                   WHERE session_id=? AND kind='message' AND role='user'""",
                (session_id,),
            ).fetchone()[0]
            if user_messages == 1:
                current_title = self._db.execute(
                    "SELECT title FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                if current_title in DEFAULT_SESSION_TITLES:
                    self._db.execute(
                        "UPDATE sessions SET title=? WHERE session_id=?",
                        (session_title_from_message(payload), session_id),
                    )
        self._db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
        return EventRecord(
            event_id=event_id,
            session_id=session_id,
            sequence=sequence,
            timestamp=now,
            kind=kind,
            role=role,
            payload=payload,
            payload_sha256=digest,
            token_estimate=estimate_tokens(serialized),
            parent_event_id=parent_event_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tags=list(tags or []),
            sensitivity=sensitivity,
            provenance=provenance,
        )

    def _bump_context_version(self, session_id: str) -> None:
        """Invalidate cached working sets for a session after any context write."""
        cursor = self._db.execute(
            "UPDATE sessions SET context_version=context_version+1 WHERE session_id=?",
            (session_id,),
        )
        if cursor.rowcount != 1:
            raise NotFoundError("session not found")

    def _claim_context_version(self, session_id: str, expected: int) -> None:
        """Atomically reserve one revision for a cross-process optimistic write."""
        cursor = self._db.execute(
            """UPDATE sessions SET context_version=context_version+1
               WHERE session_id=? AND context_version=?""",
            (session_id, expected),
        )
        if cursor.rowcount == 1:
            return
        if self._db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone() is None:
            raise NotFoundError("session not found")
        current = self._db.execute(
            "SELECT context_version FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        raise ConcurrentUpdateError(f"会话已被其他进程更新：expected={expected}, current={current}")

    @_serialized
    def context_version(self, session_id: str) -> int:
        """Return the database-backed working-set cache version for a session."""
        with self._lock:
            row = self._db.execute(
                "SELECT context_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("session not found")
        return int(row["context_version"])

    @_serialized
    def workspace_version(self, session_id: str) -> int:
        """Return the persisted workspace revision participating in prompt cache keys."""
        row = self._db.execute(
            """SELECT w.version FROM sessions s
               LEFT JOIN workspaces w ON w.workspace_id=s.workspace_id
               WHERE s.session_id=?""",
            (session_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("session not found")
        return int(row["version"] or 0)

    @_serialized
    def claim_tool_invocation(
        self, session_id: str, call_id: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Claim a durable tool invocation or return a safe replay/block decision."""
        self.get_session(session_id)
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        arguments_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO tool_invocations(
                       session_id,call_id,tool_name,arguments_sha256,status,
                       result_event_id,created_at,completed_at
                   ) VALUES(?,?,?,?, 'running', NULL, ?, NULL)""",
                (session_id, call_id, tool_name, arguments_sha256, self._now()),
            )
            row = self._db.execute(
                "SELECT * FROM tool_invocations WHERE session_id=? AND call_id=?",
                (session_id, call_id),
            ).fetchone()
        if cursor.rowcount == 1:
            return {"action": "execute", "arguments_sha256": arguments_sha256}
        if row["tool_name"] != tool_name or row["arguments_sha256"] != arguments_sha256:
            return {
                "action": "conflict",
                "arguments_sha256": arguments_sha256,
                "original_tool": row["tool_name"],
            }
        if row["status"] != "completed" or not row["result_event_id"]:
            return {"action": "uncertain", "arguments_sha256": arguments_sha256}
        original = self.get_event(row["result_event_id"])
        return {
            "action": "replay",
            "arguments_sha256": arguments_sha256,
            "result_event_id": original.event_id,
            "result": original.payload,
        }

    @_serialized
    def complete_tool_invocation(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: Any,
    ) -> EventRecord:
        """Atomically persist a tool result and mark its invocation completed."""
        with self._db:
            row = self._db.execute(
                "SELECT * FROM tool_invocations WHERE session_id=? AND call_id=?",
                (session_id, call_id),
            ).fetchone()
            if row is None:
                raise ValidationError("tool invocation was not claimed")
            if row["tool_name"] != tool_name:
                raise ValidationError("tool invocation name changed")
            if row["status"] == "completed" and row["result_event_id"]:
                return self.get_event(row["result_event_id"])
            result_event = self._insert_event(
                session_id,
                "tool_result",
                "tool",
                result,
                tool_name=tool_name,
                tool_call_id=call_id,
                provenance="local-tool-runtime",
            )
            self._db.execute(
                """UPDATE tool_invocations
                   SET status='completed', result_event_id=?, completed_at=?
                   WHERE session_id=? AND call_id=? AND status='running'""",
                (result_event.event_id, self._now(), session_id, call_id),
            )
            self._bump_context_version(session_id)
        return result_event

    def _store_payload(self, serialized: str) -> tuple[Optional[str], Optional[str]]:
        if len(serialized.encode("utf-8")) <= self._blob_threshold:
            return serialized, None
        return None, self._blobs.put(serialized)

    def _deserialize_payload(self, row: sqlite3.Row) -> Any:
        raw = (
            row["payload_inline"]
            if row["payload_inline"] is not None
            else self._blobs.get(row["payload_ref"])
        )
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def _to_event(self, row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            event_id=row["event_id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            timestamp=row["timestamp"],
            kind=row["kind"],
            role=row["role"],
            payload=self._deserialize_payload(row),
            payload_sha256=row["payload_sha256"],
            token_estimate=row["token_estimate"],
            parent_event_id=row["parent_event_id"],
            tool_name=row["tool_name"],
            tool_call_id=row["tool_call_id"],
            tags=json.loads(row["tags"]),
            sensitivity=row["sensitivity"],
            provenance=row["provenance"],
        )

    @_serialized
    def get_event(self, event_id: str) -> EventRecord:
        row = self._db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("event not found")
        return self._to_event(row)

    @_serialized
    def list_events(self, session_id: str, *, limit: int = 200, after: int = 0) -> List[EventRecord]:
        self.get_session(session_id)
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (session_id, after, max(1, min(limit, 1000))),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    @_serialized
    def recent_events(self, session_id: str, limit: int) -> List[EventRecord]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY sequence DESC LIMIT ?", (session_id, limit)
        ).fetchall()
        return [self._to_event(row) for row in reversed(rows)]

    @_serialized
    def recent_active_events(self, session_id: str, limit: int) -> List[EventRecord]:
        """Return recent events that are not covered by an archive range.

        Archives compact only the model-facing projection.  The source rows stay
        in ``events`` and therefore remain addressable through search/retrieve.
        Pinned archived events are added back separately by WorkingSetBuilder.
        """
        self.get_session(session_id)
        rows = self._db.execute(
            """SELECT e.* FROM events e
               WHERE e.session_id=?
                 AND e.kind NOT IN ('model_raw', 'archive', 'checkpoint', 'assistant_reasoning')
                 AND e.sensitivity!='internal'
                 AND NOT EXISTS (
                   SELECT 1 FROM archives a
                   WHERE a.session_id=e.session_id
                     AND e.sequence BETWEEN a.start_sequence AND a.end_sequence
               )
               ORDER BY e.sequence DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [self._to_event(row) for row in reversed(rows)]

    @_serialized
    def list_searchable_events(self, session_id: str, limit: int = 1000) -> List[EventRecord]:
        """Return a bounded recent corpus for non-persistent vector retrieval."""
        self.get_session(session_id)
        rows = self._db.execute(
            """SELECT * FROM events
               WHERE session_id=? AND sensitivity!='internal'
                 AND kind NOT IN ('model_raw', 'checkpoint', 'assistant_reasoning')
               ORDER BY sequence DESC LIMIT ?""",
            (session_id, max(1, min(limit, 5000))),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    @_serialized
    def search_events(self, session_id: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        self.get_session(session_id)
        terms = searchable_text(query).split()
        if not terms:
            return []
        # Strong tokens (jieba words, bigrams, technical tokens) beat single CJK
        # characters; the complete query phrase wins everything.
        strong = [term for term in terms if len(term) >= 2]
        weak = [term for term in terms if len(term) == 1]
        quoted = lambda term: '"' + term.replace('"', '""') + '"'  # noqa: E731
        parts = [quoted(term) for term in strong[:24]] + [quoted(term) for term in weak[:16]]
        expression = " OR ".join(parts) if parts else quoted(terms[0])
        rows = self._db.execute(
            """SELECT e.*, bm25(event_fts) AS score FROM event_fts
               JOIN events e ON e.event_id=event_fts.event_id
               WHERE event_fts MATCH ? AND e.session_id=? AND e.sensitivity!='internal'
               ORDER BY score LIMIT ?""",
            (expression, session_id, max(limit * 4, 20)),
        ).fetchall()
        normalized_query = normalize_text(query).casefold()
        weighted: List[Dict[str, Any]] = []
        for row in rows:
            event = self._to_event(row)
            serialized = normalize_text(self._serialize(event.payload)).casefold()
            phrase_hit = 1.0 if normalized_query in serialized else 0.0
            strong_hits = sum(1 for term in strong if term in serialized)
            weak_hits = sum(1 for term in weak if term in serialized)
            # Lower score = better (bm25 returns negative values in FTS5).
            score = float(row["score"]) - phrase_hit * 50.0 - strong_hits * 5.0 - weak_hits * 1.0
            weighted.append(
                {
                    "event": event.to_dict(),
                    "excerpt": excerpt(self._serialize(event.payload)),
                    "score": round(score, 2),
                }
            )
        return sorted(weighted, key=lambda item: item["score"])[:limit]

    @_serialized
    def pin_event(self, session_id: str, event_id: str, rationale: str) -> None:
        self.pin_events(session_id, [event_id], rationale)

    @_serialized
    def pin_events(self, session_id: str, event_ids: Sequence[str], rationale: str) -> None:
        """Atomically validate and pin a batch of events."""
        unique_ids = list(dict.fromkeys(event_ids))
        events = [self.get_event(event_id) for event_id in unique_ids]
        if any(event.session_id != session_id for event in events):
            raise ValidationError("event belongs to another session")
        with self._lock, self._db:
            now = self._now()
            self._db.executemany(
                "INSERT OR REPLACE INTO pins(session_id,event_id,rationale,created_at) VALUES(?,?,?,?)",
                [(session_id, event_id, rationale, now) for event_id in unique_ids],
            )
            if unique_ids:
                self._bump_context_version(session_id)

    @_serialized
    def unpin_event(self, session_id: str, event_id: str) -> None:
        self.unpin_events(session_id, [event_id])

    @_serialized
    def unpin_events(self, session_id: str, event_ids: Sequence[str]) -> None:
        unique_ids = list(dict.fromkeys(event_ids))
        with self._lock, self._db:
            self._db.executemany(
                "DELETE FROM pins WHERE session_id=? AND event_id=?",
                [(session_id, event_id) for event_id in unique_ids],
            )
            if unique_ids:
                self._bump_context_version(session_id)

    @_serialized
    def pinned_event_ids(self, session_id: str) -> set[str]:
        rows = self._db.execute("SELECT event_id FROM pins WHERE session_id=?", (session_id,)).fetchall()
        return {row["event_id"] for row in rows}

    @_serialized
    def pinned_events(self, session_id: str) -> List[EventRecord]:
        rows = self._db.execute(
            """SELECT e.* FROM pins p JOIN events e ON e.event_id=p.event_id
               WHERE p.session_id=? ORDER BY e.sequence""",
            (session_id,),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    @_serialized
    def pinned_token_total(self, session_id: str) -> int:
        row = self._db.execute(
            """SELECT COALESCE(SUM(e.token_estimate), 0) AS total
               FROM pins p JOIN events e ON e.event_id=p.event_id
               WHERE p.session_id=?
                 AND e.kind NOT IN ('model_raw', 'archive', 'assistant_reasoning')
                 AND e.sensitivity!='internal'""",
            (session_id,),
        ).fetchone()
        return int(row["total"])

    @_serialized
    def create_archive(
        self, session_id: str, start_sequence: int, end_sequence: int, reason: str, state: Dict[str, Any]
    ) -> Dict[str, Any]:
        if start_sequence < 1 or end_sequence < start_sequence:
            raise ValidationError("invalid event range")
        source_rows = self._db.execute(
            """SELECT e.event_id FROM events e
               WHERE e.session_id=? AND e.sequence BETWEEN ? AND ? AND e.kind!='archive'
                 AND NOT EXISTS (
                     SELECT 1 FROM archives a
                     WHERE a.session_id=e.session_id
                       AND e.sequence BETWEEN a.start_sequence AND a.end_sequence
                 )
               ORDER BY e.sequence""",
            (session_id, start_sequence, end_sequence),
        ).fetchall()
        if not source_rows:
            raise ValidationError("event range is empty or already archived")
        archive_id = "arc_" + uuid.uuid4().hex
        summary = {
            "archive_id": archive_id,
            "reason": reason,
            "event_range": [start_sequence, end_sequence],
            "source_event_ids": [row[0] for row in source_rows],
            "state": state,
        }
        # The summary event and the archives row must land in ONE transaction:
        # a failure halfway would otherwise leave an orphan archive event whose
        # latest_archive no longer matches the event stream.
        with self._lock, self._db:
            summary_event = self._insert_event(session_id, "archive", "system", summary, tags=["archive"])
            self._db.execute(
                """INSERT INTO archives(
                       archive_id,session_id,start_sequence,end_sequence,
                       summary_event_id,reason,state_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    archive_id,
                    session_id,
                    start_sequence,
                    end_sequence,
                    summary_event.event_id,
                    reason,
                    json.dumps(state, ensure_ascii=False),
                    self._now(),
                ),
            )
            self._bump_context_version(session_id)
        return {
            "archive_id": archive_id,
            "summary_event_id": summary_event.event_id,
            "event_range": [start_sequence, end_sequence],
            "state": state,
        }

    @_serialized
    def latest_archive(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM archives WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    @_serialized
    def create_checkpoint(
        self,
        session_id: str,
        trigger_event_id: str,
        reason: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist a runtime-owned recovery checkpoint and its audit event atomically."""
        trigger = self.get_event(trigger_event_id)
        if trigger.session_id != session_id:
            raise ValidationError("checkpoint trigger belongs to another session")
        checkpoint_id = "chk_" + uuid.uuid4().hex
        payload = {"checkpoint_id": checkpoint_id, "reason": reason, "state": state}
        created_at = self._now()
        with self._lock, self._db:
            checkpoint_event = self._insert_event(
                session_id,
                "checkpoint",
                "system",
                payload,
                parent_event_id=trigger_event_id,
                tags=["checkpoint", "recoverable"],
                provenance="agent-runtime",
            )
            self._db.execute(
                """INSERT INTO checkpoints(
                       checkpoint_id,session_id,trigger_event_id,checkpoint_event_id,
                       reason,state_json,created_at,resolved_at,resolution_event_id
                   ) VALUES(?,?,?,?,?,?,?,NULL,NULL)""",
                (
                    checkpoint_id,
                    session_id,
                    trigger_event_id,
                    checkpoint_event.event_id,
                    reason,
                    json.dumps(state, ensure_ascii=False),
                    created_at,
                ),
            )
            # A newer pause state supersedes every older unresolved checkpoint.
            # This keeps continuation injection single-valued across repeated limits.
            self._db.execute(
                """UPDATE checkpoints SET resolved_at=?, resolution_event_id=?
                   WHERE session_id=? AND checkpoint_id!=? AND resolved_at IS NULL""",
                (created_at, checkpoint_event.event_id, session_id, checkpoint_id),
            )
            self._bump_context_version(session_id)
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_event_id": checkpoint_event.event_id,
            "trigger_event_id": trigger_event_id,
            "reason": reason,
            "state": state,
            "created_at": created_at,
        }

    @_serialized
    def latest_checkpoint(self, session_id: str, *, active_only: bool = False) -> Optional[Dict[str, Any]]:
        self.get_session(session_id)
        condition = " AND resolved_at IS NULL" if active_only else ""
        row = self._db.execute(
            f"SELECT * FROM checkpoints WHERE session_id=?{condition} ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    @_serialized
    def resolve_active_checkpoint(
        self, session_id: str, resolution_event_id: str
    ) -> Optional[Dict[str, Any]]:
        """Mark the newest paused checkpoint resolved after a successful continuation."""
        resolution = self.get_event(resolution_event_id)
        if resolution.session_id != session_id:
            raise ValidationError("checkpoint resolution belongs to another session")
        if self.latest_checkpoint(session_id, active_only=True) is None:
            return None
        with self._lock, self._db:
            self._db.execute(
                """UPDATE checkpoints SET resolved_at=?, resolution_event_id=?
                   WHERE session_id=? AND resolved_at IS NULL""",
                (self._now(), resolution_event_id, session_id),
            )
            self._bump_context_version(session_id)
        return self.latest_checkpoint(session_id)

    @_serialized
    def archive_stats(self, session_id: str) -> Dict[str, int]:
        """Count distinct source events currently externalized by archives."""
        row = self._db.execute(
            """SELECT COUNT(*) AS count, COALESCE(SUM(e.token_estimate), 0) AS tokens
               FROM events e
               WHERE e.session_id=? AND e.kind!='archive' AND EXISTS (
                   SELECT 1 FROM archives a
                   WHERE a.session_id=e.session_id
                     AND e.sequence BETWEEN a.start_sequence AND a.end_sequence
               )""",
            (session_id,),
        ).fetchone()
        return {"count": int(row["count"]), "tokens": int(row["tokens"])}

    @_serialized
    def session_stats(self, session_id: str) -> Dict[str, int]:
        self.get_session(session_id)
        row = self._db.execute(
            """SELECT COUNT(*) AS count, COALESCE(SUM(token_estimate),0) AS tokens,
               COALESCE(MAX(sequence),0) AS latest FROM events WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        return dict(row)

    # --- long-term memory --------------------------------------------------

    def _bump_memory_version(self) -> None:
        self._db.execute("UPDATE metadata SET value=value+1 WHERE key='memory_version'")

    @_serialized
    def memory_version(self) -> int:
        row = self._db.execute("SELECT value FROM metadata WHERE key='memory_version'").fetchone()
        return int(row["value"] if row else 0)

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["pinned"] = bool(result["pinned"])
        return result

    @_serialized
    def get_memory(self, memory_id: str) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None:
            raise NotFoundError("memory not found")
        result = self._memory_row(row)
        sources = self._db.execute(
            "SELECT event_id FROM memory_sources WHERE memory_id=? ORDER BY event_id",
            (memory_id,),
        ).fetchall()
        result["source_event_ids"] = [item["event_id"] for item in sources]
        return result

    @_serialized
    def find_active_memory(
        self, scope_type: str, scope_id: str, memory_type: str, memory_key: str
    ) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            """SELECT memory_id FROM memories
               WHERE scope_type=? AND scope_id=? AND memory_type=? AND memory_key=?
                 AND status='active' LIMIT 1""",
            (scope_type, scope_id, memory_type, memory_key),
        ).fetchone()
        return self.get_memory(row["memory_id"]) if row else None

    @_serialized
    def create_memory(
        self,
        *,
        scope_type: str,
        scope_id: str,
        memory_type: str,
        memory_key: str,
        content: str,
        confidence: float,
        confirmed: bool,
        pinned: bool,
        created_reason: str,
        source_session_id: str,
        source_event_ids: Sequence[str],
        expires_at: Optional[str],
        terms: Dict[str, float],
    ) -> Dict[str, Any]:
        self.get_session(source_session_id)
        memory_id = "mem_" + uuid.uuid4().hex
        now = self._now()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            active_row = self._db.execute(
                """SELECT memory_id FROM memories
                   WHERE scope_type=? AND scope_id=? AND memory_type=? AND memory_key=?
                     AND status='active' LIMIT 1""",
                (scope_type, scope_id, memory_type, memory_key),
            ).fetchone()
            existing = self.get_memory(active_row["memory_id"]) if active_row else None
            if existing and existing["content_sha256"] == digest:
                self._reinforce_memory_in_transaction(existing, source_event_ids, confidence, now)
                self._db.commit()
                return {
                    "memory": self.get_memory(existing["memory_id"]),
                    "outcome": "already_active",
                    "conflict_memory_id": None,
                }

            candidate_row = self._db.execute(
                """SELECT memory_id FROM memories
                   WHERE scope_type=? AND scope_id=? AND memory_type=? AND memory_key=?
                     AND content_sha256=? AND status='candidate'
                   ORDER BY created_at DESC LIMIT 1""",
                (scope_type, scope_id, memory_type, memory_key, digest),
            ).fetchone()
            if candidate_row:
                candidate = self.get_memory(candidate_row["memory_id"])
                self._reinforce_memory_in_transaction(candidate, source_event_ids, confidence, now)
                self._db.commit()
                return {
                    "memory": self.get_memory(candidate["memory_id"]),
                    "outcome": "already_candidate",
                    "conflict_memory_id": existing["memory_id"] if existing else None,
                }

            status = "active" if confirmed and existing is None else "candidate"
            self._db.execute(
                """INSERT INTO memories(
                       memory_id,scope_type,scope_id,memory_type,memory_key,content,
                       content_sha256,confidence,status,pinned,created_reason,
                       source_session_id,supersedes_memory_id,created_at,updated_at,
                       last_verified_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",
                (
                    memory_id,
                    scope_type,
                    scope_id,
                    memory_type,
                    memory_key,
                    content,
                    digest,
                    confidence,
                    status,
                    int(pinned),
                    created_reason,
                    source_session_id,
                    now,
                    now,
                    now,
                    expires_at,
                ),
            )
            self._db.executemany(
                "INSERT INTO memory_sources(memory_id,event_id) VALUES(?,?)",
                [(memory_id, event_id) for event_id in source_event_ids],
            )
            self._db.executemany(
                "INSERT INTO memory_terms(memory_id,term,weight) VALUES(?,?,?)",
                [(memory_id, term, weight) for term, weight in terms.items()],
            )
            if status == "active":
                self._db.execute(
                    "INSERT INTO memory_fts(memory_id,scope_type,scope_id,search_text) VALUES(?,?,?,?)",
                    (memory_id, scope_type, scope_id, searchable_text(f"{memory_key} {content}")),
                )
            self._insert_memory_audit(memory_id, "created", digest, {"status": status})
            self._bump_memory_version()
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return {
            "memory": self.get_memory(memory_id),
            "outcome": "active" if status == "active" else "candidate",
            "conflict_memory_id": existing["memory_id"] if existing else None,
        }

    def _reinforce_memory_in_transaction(
        self,
        memory: Dict[str, Any],
        source_event_ids: Sequence[str],
        confidence: float,
        now: str,
    ) -> None:
        self._db.executemany(
            "INSERT OR IGNORE INTO memory_sources(memory_id,event_id) VALUES(?,?)",
            [(memory["memory_id"], event_id) for event_id in source_event_ids],
        )
        self._db.execute(
            """UPDATE memories SET confidence=MAX(confidence,?),updated_at=?,
               last_verified_at=? WHERE memory_id=?""",
            (confidence, now, now, memory["memory_id"]),
        )
        self._insert_memory_audit(
            memory["memory_id"],
            "reinforced",
            memory["content_sha256"],
            {"source_event_ids": list(source_event_ids)},
        )
        self._bump_memory_version()

    @_serialized
    def activate_memory(
        self, memory_id: str, *, supersedes_memory_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            memory = self.get_memory(memory_id)
            if memory["status"] == "active":
                if memory.get("supersedes_memory_id") == supersedes_memory_id:
                    self._db.commit()
                    return memory
                raise ConcurrentUpdateError("memory was already activated by another request")
            if memory["status"] != "candidate":
                raise ValidationError("only a candidate memory can be activated")
            now = self._now()
            if supersedes_memory_id:
                previous = self.get_memory(supersedes_memory_id)
                if (
                    previous["scope_type"],
                    previous["scope_id"],
                    previous["memory_type"],
                    previous["memory_key"],
                ) != (
                    memory["scope_type"],
                    memory["scope_id"],
                    memory["memory_type"],
                    memory["memory_key"],
                ):
                    raise ValidationError("superseded memory has a different identity")
                if previous["status"] != "active":
                    raise ConcurrentUpdateError(
                        "superseded memory is no longer active; refresh candidates before confirming"
                    )
                self._db.execute(
                    "UPDATE memories SET status='superseded',updated_at=? WHERE memory_id=?",
                    (now, supersedes_memory_id),
                )
                self._db.execute("DELETE FROM memory_fts WHERE memory_id=?", (supersedes_memory_id,))
            else:
                existing = self.find_active_memory(
                    memory["scope_type"],
                    memory["scope_id"],
                    memory["memory_type"],
                    memory["memory_key"],
                )
                if existing is not None and existing["memory_id"] != memory_id:
                    raise ValidationError(
                        "memory conflicts with an active value; supersedes_memory_id required"
                    )
            self._db.execute(
                """UPDATE memories SET status='active',supersedes_memory_id=?,
                   updated_at=?,last_verified_at=? WHERE memory_id=?""",
                (supersedes_memory_id, now, now, memory_id),
            )
            self._db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            self._db.execute(
                "INSERT INTO memory_fts(memory_id,scope_type,scope_id,search_text) VALUES(?,?,?,?)",
                (
                    memory_id,
                    memory["scope_type"],
                    memory["scope_id"],
                    searchable_text(f"{memory['memory_key']} {memory['content']}"),
                ),
            )
            self._insert_memory_audit(
                memory_id,
                "activated",
                memory["content_sha256"],
                {"supersedes_memory_id": supersedes_memory_id},
            )
            self._bump_memory_version()
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_memory(memory_id)

    @_serialized
    def forget_memory(self, memory_id: str, reason: str) -> Dict[str, Any]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            memory = self.get_memory(memory_id)
            if memory["status"] == "deleted":
                self._db.commit()
                return memory
            now = self._now()
            self._db.execute(
                """UPDATE memories SET content='',status='deleted',pinned=0,
                   updated_at=? WHERE memory_id=?""",
                (now, memory_id),
            )
            self._db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            self._db.execute("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))
            self._insert_memory_audit(memory_id, "deleted", memory["content_sha256"], {"reason": reason})
            self._bump_memory_version()
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_memory(memory_id)

    @_serialized
    def set_memory_pinned(
        self,
        memory_id: str,
        pinned: bool,
        *,
        expected_pinned: bool,
    ) -> Dict[str, Any]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            memory = self.get_memory(memory_id)
            if memory["status"] != "active":
                raise ValidationError("only active memory can be pinned")
            if memory["pinned"] == pinned:
                self._db.commit()
                return memory
            if memory["pinned"] != expected_pinned:
                raise ConcurrentUpdateError("memory pin state changed; refresh memories before updating")
            now = self._now()
            self._db.execute(
                "UPDATE memories SET pinned=?,updated_at=? WHERE memory_id=?",
                (int(pinned), now, memory_id),
            )
            self._insert_memory_audit(
                memory_id,
                "pinned" if pinned else "unpinned",
                memory["content_sha256"],
                {"expected_pinned": expected_pinned},
            )
            self._bump_memory_version()
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_memory(memory_id)

    @_serialized
    def list_memories(
        self,
        scope_pairs: Sequence[tuple[str, str]],
        *,
        statuses: Sequence[str] = ("active",),
        pinned_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if not scope_pairs or not statuses:
            return []
        scope_sql = " OR ".join("(scope_type=? AND scope_id=?)" for _ in scope_pairs)
        status_sql = ",".join("?" for _ in statuses)
        pinned_sql = " AND pinned=1" if pinned_only else ""
        params: list[Any] = [value for pair in scope_pairs for value in pair]
        params.extend(statuses)
        params.append(self._now())
        params.append(max(1, min(limit, 500)))
        rows = self._db.execute(
            f"""SELECT memory_id FROM memories WHERE ({scope_sql})
                 AND status IN ({status_sql}){pinned_sql}
                 AND (expires_at IS NULL OR expires_at>?)
                 ORDER BY pinned DESC, updated_at DESC LIMIT ?""",
            params,
        ).fetchall()
        return [self.get_memory(row["memory_id"]) for row in rows]

    @_serialized
    def expire_memories(self, scope_pairs: Sequence[tuple[str, str]]) -> int:
        if not scope_pairs:
            return 0
        scope_sql = " OR ".join("(scope_type=? AND scope_id=?)" for _ in scope_pairs)
        params: list[Any] = [value for pair in scope_pairs for value in pair]
        now = self._now()
        params.append(now)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # The selection must happen after obtaining the write lock.  Two
            # Core processes may notice the same deadline concurrently; only
            # the first one is allowed to transition and audit the rows.
            rows = self._db.execute(
                f"""SELECT memory_id,content_sha256 FROM memories WHERE ({scope_sql})
                     AND status IN ('active','candidate') AND expires_at IS NOT NULL
                     AND expires_at<=?""",
                params,
            ).fetchall()
            if not rows:
                self._db.commit()
                return 0
            memory_ids = [row["memory_id"] for row in rows]
            self._db.executemany(
                "UPDATE memories SET status='expired',pinned=0,updated_at=? WHERE memory_id=?",
                [(now, memory_id) for memory_id in memory_ids],
            )
            self._db.executemany(
                "DELETE FROM memory_fts WHERE memory_id=?",
                [(memory_id,) for memory_id in memory_ids],
            )
            self._db.executemany(
                "DELETE FROM memory_terms WHERE memory_id=?",
                [(memory_id,) for memory_id in memory_ids],
            )
            for row in rows:
                self._insert_memory_audit(
                    row["memory_id"], "expired", row["content_sha256"], {"expired_at": now}
                )
            self._bump_memory_version()
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return len(memory_ids)

    @_serialized
    def search_memory_lexical(
        self, scope_pairs: Sequence[tuple[str, str]], query: str, limit: int
    ) -> List[Dict[str, Any]]:
        terms = searchable_text(query).split()
        if not terms or not scope_pairs:
            return []
        quoted = lambda term: '"' + term.replace('"', '""') + '"'  # noqa: E731
        expression = " OR ".join(quoted(term) for term in terms[:40])
        scope_sql = " OR ".join("(m.scope_type=? AND m.scope_id=?)" for _ in scope_pairs)
        params: list[Any] = [expression]
        params.extend(value for pair in scope_pairs for value in pair)
        params.append(max(limit, 20))
        rows = self._db.execute(
            f"""SELECT m.memory_id,bm25(memory_fts) AS score FROM memory_fts
                 JOIN memories m ON m.memory_id=memory_fts.memory_id
                 WHERE memory_fts MATCH ? AND ({scope_sql}) AND m.status='active'
                   AND (m.expires_at IS NULL OR m.expires_at>?)
                 ORDER BY score LIMIT ?""",
            [*params[:-1], self._now(), params[-1]],
        ).fetchall()
        return [{"memory": self.get_memory(row["memory_id"]), "score": float(row["score"])} for row in rows]

    @_serialized
    def search_memory_terms(
        self,
        scope_pairs: Sequence[tuple[str, str]],
        query_terms: Dict[str, float],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Search the persisted sparse term index without loading memory bodies."""
        if not scope_pairs or not query_terms:
            return []
        term_sql = ",".join("?" for _ in query_terms)
        scope_sql = " OR ".join("(m.scope_type=? AND m.scope_id=?)" for _ in scope_pairs)
        params: list[Any] = list(query_terms)
        params.extend(value for pair in scope_pairs for value in pair)
        params.append(self._now())
        rows = self._db.execute(
            f"""SELECT mt.memory_id,mt.term,mt.weight FROM memory_terms mt
                 JOIN memories m ON m.memory_id=mt.memory_id
                 WHERE mt.term IN ({term_sql}) AND ({scope_sql}) AND m.status='active'
                   AND (m.expires_at IS NULL OR m.expires_at>?)""",
            params,
        ).fetchall()
        scores: Dict[str, float] = {}
        matched: Dict[str, set[str]] = {}
        for row in rows:
            scores[row["memory_id"]] = scores.get(row["memory_id"], 0.0) + (
                float(row["weight"]) * float(query_terms[row["term"]])
            )
            matched.setdefault(row["memory_id"], set()).add(row["term"])
        query_weight = sum(abs(float(weight)) for weight in query_terms.values()) or 1.0
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [
            {
                "memory": self.get_memory(memory_id),
                "score": round(score, 6),
                "query_coverage": round(
                    sum(abs(float(query_terms[term])) for term in matched[memory_id]) / query_weight,
                    4,
                ),
                "matched_terms": sorted(matched[memory_id])[:24],
            }
            for memory_id, score in ranked
        ]

    def _insert_memory_audit(
        self, memory_id: str, action: str, content_sha256: str, details: Dict[str, Any]
    ) -> None:
        self._db.execute(
            """INSERT INTO memory_audit(
                   audit_id,memory_id,action,content_sha256,details_json,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                "maud_" + uuid.uuid4().hex,
                memory_id,
                action,
                content_sha256,
                json.dumps(details, ensure_ascii=False),
                self._now(),
            ),
        )

    @_serialized
    def list_memory_audit(self, memory_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        self.get_memory(memory_id)
        rows = self._db.execute(
            """SELECT audit_id,memory_id,action,content_sha256,details_json,created_at
               FROM memory_audit WHERE memory_id=? ORDER BY created_at DESC LIMIT ?""",
            (memory_id, max(1, min(limit, 500))),
        ).fetchall()
        audit = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            audit.append(item)
        return audit

    # --- permission broker -------------------------------------------------

    @_serialized
    def create_permission_request(
        self,
        session_id: Optional[str],
        subject_type: str,
        subject_id: str,
        action: str,
        arguments_sha256: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        if session_id:
            self.get_session(session_id)
        existing = self._db.execute(
            """SELECT * FROM permission_requests
               WHERE status='pending' AND session_id IS ? AND subject_type=?
                 AND subject_id=? AND action=? AND arguments_sha256=?
               ORDER BY created_at DESC LIMIT 1""",
            (session_id, subject_type, subject_id, action, arguments_sha256),
        ).fetchone()
        if existing is not None:
            return self._permission_request(existing)
        request_id = "prm_" + uuid.uuid4().hex
        created_at = self._now()
        with self._db:
            self._db.execute(
                """INSERT INTO permission_requests(
                       request_id,session_id,subject_type,subject_id,action,arguments_sha256,
                       details_json,status,scope,created_at,decided_at,consumed_at
                   ) VALUES(?,?,?,?,?,?,?,'pending',NULL,?,NULL,NULL)""",
                (
                    request_id,
                    session_id,
                    subject_type,
                    subject_id,
                    action,
                    arguments_sha256,
                    json.dumps(details, ensure_ascii=False),
                    created_at,
                ),
            )
            self._insert_permission_audit(
                request_id,
                session_id,
                subject_type,
                subject_id,
                action,
                "pending",
                "agent",
            )
        return self.get_permission_request(request_id)

    @_serialized
    def get_permission_request(self, request_id: str) -> Dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM permission_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("permission request not found")
        return self._permission_request(row)

    @_serialized
    def list_permission_requests(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self._db.execute(
                "SELECT * FROM permission_requests WHERE status=? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM permission_requests ORDER BY created_at DESC").fetchall()
        return [self._permission_request(row) for row in rows]

    @_serialized
    def decide_permission_request(
        self, request_id: str, decision: str, scope: str = "once"
    ) -> Dict[str, Any]:
        if decision not in {"approved", "denied"}:
            raise ValidationError("permission decision must be approved or denied")
        if scope not in {"once", "session", "always"}:
            raise ValidationError("permission scope must be once, session, or always")
        request = self.get_permission_request(request_id)
        if request["status"] != "pending":
            raise ValidationError("permission request was already decided")
        if scope == "session" and not request["session_id"]:
            raise ValidationError("session permission requires a session")
        decided_at = self._now()
        with self._db:
            self._db.execute(
                """UPDATE permission_requests SET status=?, scope=?, decided_at=?
                   WHERE request_id=? AND status='pending'""",
                (decision, scope, decided_at, request_id),
            )
            if decision == "approved" and scope in {"session", "always"}:
                self._db.execute(
                    """INSERT INTO permission_grants(
                           grant_id,subject_type,subject_id,action,scope,session_id,created_at,revoked_at
                       ) VALUES(?,?,?,?,?,?,?,NULL)""",
                    (
                        "grt_" + uuid.uuid4().hex,
                        request["subject_type"],
                        request["subject_id"],
                        request["action"],
                        scope,
                        request["session_id"] if scope == "session" else None,
                        decided_at,
                    ),
                )
            self._insert_permission_audit(
                request_id,
                request["session_id"],
                request["subject_type"],
                request["subject_id"],
                request["action"],
                decision,
                "user",
            )
        return self.get_permission_request(request_id)

    @_serialized
    def consume_permission(
        self,
        session_id: Optional[str],
        subject_type: str,
        subject_id: str,
        action: str,
        arguments_sha256: str,
    ) -> Optional[Dict[str, Any]]:
        grant = self._db.execute(
            """SELECT * FROM permission_grants
               WHERE subject_type=? AND subject_id=? AND action=? AND revoked_at IS NULL
                 AND (scope='always' OR (scope='session' AND session_id=?))
               ORDER BY CASE scope WHEN 'session' THEN 0 ELSE 1 END, created_at DESC LIMIT 1""",
            (subject_type, subject_id, action, session_id),
        ).fetchone()
        if grant is not None:
            with self._db:
                self._insert_permission_audit(
                    None,
                    session_id,
                    subject_type,
                    subject_id,
                    action,
                    "allowed",
                    f"grant:{grant['scope']}",
                )
            return {"source": "grant", "scope": grant["scope"], "grant_id": grant["grant_id"]}
        request = self._db.execute(
            """SELECT * FROM permission_requests
               WHERE status='approved' AND scope='once' AND consumed_at IS NULL
                 AND session_id IS ? AND subject_type=? AND subject_id=? AND action=?
                 AND arguments_sha256=? ORDER BY decided_at DESC LIMIT 1""",
            (session_id, subject_type, subject_id, action, arguments_sha256),
        ).fetchone()
        if request is None:
            return None
        consumed_at = self._now()
        with self._db:
            self._db.execute(
                "UPDATE permission_requests SET status='consumed', consumed_at=? WHERE request_id=?",
                (consumed_at, request["request_id"]),
            )
            self._insert_permission_audit(
                request["request_id"],
                session_id,
                subject_type,
                subject_id,
                action,
                "allowed",
                "grant:once",
            )
        return {"source": "request", "scope": "once", "request_id": request["request_id"]}

    @_serialized
    def revoke_permission_grant(self, grant_id: str) -> bool:
        grant = self._db.execute(
            "SELECT * FROM permission_grants WHERE grant_id=? AND revoked_at IS NULL",
            (grant_id,),
        ).fetchone()
        if grant is None:
            return False
        with self._db:
            cursor = self._db.execute(
                "UPDATE permission_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                (self._now(), grant_id),
            )
            self._insert_permission_audit(
                None,
                grant["session_id"],
                grant["subject_type"],
                grant["subject_id"],
                grant["action"],
                "revoked",
                "user",
            )
        return cursor.rowcount == 1

    @_serialized
    def list_permission_grants(self) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM permission_grants WHERE revoked_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def list_permission_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM permission_audit ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_permission_audit(
        self,
        request_id: Optional[str],
        session_id: Optional[str],
        subject_type: str,
        subject_id: str,
        action: str,
        outcome: str,
        source: str,
    ) -> None:
        self._db.execute(
            """INSERT INTO permission_audit(
                   audit_id,request_id,session_id,subject_type,subject_id,action,outcome,source,timestamp
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "aud_" + uuid.uuid4().hex,
                request_id,
                session_id,
                subject_type,
                subject_id,
                action,
                outcome,
                source,
                self._now(),
            ),
        )

    @staticmethod
    def _permission_request(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    @_serialized
    def close(self) -> None:
        self._db.close()
