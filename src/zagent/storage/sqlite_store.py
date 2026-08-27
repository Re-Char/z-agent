from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from zagent.context.tokenization import estimate_tokens, excerpt, searchable_text
from zagent.domain.errors import NotFoundError, ValidationError
from zagent.domain.models import EventRecord

from .blob_store import BlobStore
from .schema import MIGRATIONS_SQL, SCHEMA_SQL


class SqliteStore:
    """Append-only session/event repository with addressable external blobs."""

    def __init__(self, data_dir: str, blob_threshold: int = 32_768) -> None:
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self._blob_threshold = blob_threshold
        self._blobs = BlobStore(root / "blobs")
        self._lock = threading.RLock()
        self._context_version: Dict[str, int] = defaultdict(int)
        self._db = sqlite3.connect(str(root / "state.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._db:
            self._db.executescript(SCHEMA_SQL)
            for statement in MIGRATIONS_SQL:
                # Column already present on fresh databases created with v2 schema.
                with contextlib.suppress(sqlite3.OperationalError):
                    self._db.execute(statement)
        self._ensure_default_workspace()

    def _ensure_default_workspace(self) -> None:
        row = self._db.execute(
            "SELECT workspace_id FROM workspaces ORDER BY created_at LIMIT 1"
        ).fetchone()
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

    def create_workspace(self, name: str, path: str = "") -> Dict[str, Any]:
        workspace_id = "ws_" + uuid.uuid4().hex
        now = self._now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO workspaces(workspace_id,name,path,created_at) VALUES(?,?,?,?)",
                (workspace_id, name.strip() or "未命名工作区", path.strip(), now),
            )
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id: str) -> Dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("workspace not found")
        result = dict(row)
        result["session_count"] = self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE workspace_id=?", (workspace_id,)
        ).fetchone()[0]
        return result

    def update_workspace(
        self, workspace_id: str, name: Optional[str] = None, path: Optional[str] = None
    ) -> Dict[str, Any]:
        self.get_workspace(workspace_id)
        if name is not None:
            clean_name = name.strip() or "未命名工作区"
            self._db.execute(
                "UPDATE workspaces SET name=? WHERE workspace_id=?", (clean_name, workspace_id)
            )
        if path is not None:
            self._db.execute(
                "UPDATE workspaces SET path=? WHERE workspace_id=?", (path.strip(), workspace_id)
            )
        return self.get_workspace(workspace_id)

    def list_workspaces(self) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            """SELECT w.*, COUNT(s.session_id) AS session_count
               FROM workspaces w LEFT JOIN sessions s ON s.workspace_id=w.workspace_id
               GROUP BY w.workspace_id ORDER BY w.created_at"""
        ).fetchall()
        return [dict(row) for row in rows]

    def default_workspace_id(self) -> str:
        row = self._db.execute(
            "SELECT workspace_id FROM workspaces ORDER BY created_at LIMIT 1"
        ).fetchone()
        return row["workspace_id"] if row else self.create_workspace("默认工作区", "")["workspace_id"]

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

    def get_session(self, session_id: str) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("session not found")
        result = dict(row)
        result["event_count"] = self._db.execute(
            "SELECT COUNT(*) FROM events WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        return result

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
    ) -> EventRecord:
        with self._lock, self._db:
            event = self._insert_event(
                session_id, kind, role, payload,
                parent_event_id=parent_event_id, tool_name=tool_name, tool_call_id=tool_call_id,
                tags=tags, sensitivity=sensitivity, provenance=provenance,
            )
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
            (event_id, session_id, sequence, now, kind, role, inline, reference, digest,
             estimate_tokens(serialized), parent_event_id, tool_name, tool_call_id,
             json.dumps(list(tags or []), ensure_ascii=False), sensitivity, provenance),
        )
        self._db.execute(
            "INSERT INTO event_fts(event_id,session_id,search_text) VALUES(?,?,?)",
            (event_id, session_id, searchable_text(serialized)),
        )
        self._db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
        return EventRecord(
            event_id=event_id, session_id=session_id, sequence=sequence, timestamp=now,
            kind=kind, role=role, payload=payload, payload_sha256=digest,
            token_estimate=estimate_tokens(serialized), parent_event_id=parent_event_id,
            tool_name=tool_name, tool_call_id=tool_call_id,
            tags=list(tags or []), sensitivity=sensitivity, provenance=provenance,
        )

    def _bump_context_version(self, session_id: str) -> None:
        """Invalidate cached working sets for a session after any context write."""
        self._context_version[session_id] += 1

    def context_version(self, session_id: str) -> int:
        """Monotonic version used by WorkingSetBuilder to cache builds per session."""
        return self._context_version.get(session_id, 0)

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
            event_id=row["event_id"], session_id=row["session_id"], sequence=row["sequence"],
            timestamp=row["timestamp"], kind=row["kind"], role=row["role"],
            payload=self._deserialize_payload(row), payload_sha256=row["payload_sha256"],
            token_estimate=row["token_estimate"], parent_event_id=row["parent_event_id"],
            tool_name=row["tool_name"], tool_call_id=row["tool_call_id"],
            tags=json.loads(row["tags"]), sensitivity=row["sensitivity"], provenance=row["provenance"],
        )

    def get_event(self, event_id: str) -> EventRecord:
        row = self._db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("event not found")
        return self._to_event(row)

    def list_events(self, session_id: str, *, limit: int = 200, after: int = 0) -> List[EventRecord]:
        self.get_session(session_id)
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (session_id, after, max(1, min(limit, 1000))),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    def recent_events(self, session_id: str, limit: int) -> List[EventRecord]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY sequence DESC LIMIT ?", (session_id, limit)
        ).fetchall()
        return [self._to_event(row) for row in reversed(rows)]

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
        normalized_query = query.casefold()
        weighted: List[Dict[str, Any]] = []
        for row in rows:
            event = self._to_event(row)
            serialized = self._serialize(event.payload).casefold()
            phrase_hit = 1.0 if normalized_query in serialized else 0.0
            strong_hits = sum(1 for term in strong if term in serialized)
            weak_hits = sum(1 for term in weak if term in serialized)
            # Lower score = better (bm25 returns negative values in FTS5).
            score = float(row["score"]) - phrase_hit * 50.0 - strong_hits * 5.0 - weak_hits * 1.0
            weighted.append({
                "event": event.to_dict(),
                "excerpt": excerpt(self._serialize(event.payload)),
                "score": round(score, 2),
            })
        return sorted(weighted, key=lambda item: item["score"])[:limit]

    def pin_event(self, session_id: str, event_id: str, rationale: str) -> None:
        event = self.get_event(event_id)
        if event.session_id != session_id:
            raise ValidationError("event belongs to another session")
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO pins(session_id,event_id,rationale,created_at) VALUES(?,?,?,?)",
                (session_id, event_id, rationale, self._now()),
            )
            self._bump_context_version(session_id)

    def unpin_event(self, session_id: str, event_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM pins WHERE session_id=? AND event_id=?", (session_id, event_id))
            self._bump_context_version(session_id)

    def pinned_events(self, session_id: str) -> List[EventRecord]:
        rows = self._db.execute(
            """SELECT e.* FROM pins p JOIN events e ON e.event_id=p.event_id
               WHERE p.session_id=? ORDER BY e.sequence""",
            (session_id,),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    def pinned_token_total(self, session_id: str) -> int:
        row = self._db.execute(
            """SELECT COALESCE(SUM(e.token_estimate), 0) AS total
               FROM pins p JOIN events e ON e.event_id=p.event_id
               WHERE p.session_id=?""",
            (session_id,),
        ).fetchone()
        return int(row["total"])

    def create_archive(
        self, session_id: str, start_sequence: int, end_sequence: int, reason: str, state: Dict[str, Any]
    ) -> Dict[str, Any]:
        if start_sequence < 1 or end_sequence < start_sequence:
            raise ValidationError("invalid event range")
        source_rows = self._db.execute(
            "SELECT event_id FROM events WHERE session_id=? AND sequence BETWEEN ? AND ? ORDER BY sequence",
            (session_id, start_sequence, end_sequence),
        ).fetchall()
        if not source_rows:
            raise ValidationError("event range is empty")
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
            summary_event = self._insert_event(
                session_id, "archive", "system", summary, tags=["archive"]
            )
            self._db.execute(
                "INSERT INTO archives VALUES(?,?,?,?,?,?,?,?)",
                (archive_id, session_id, start_sequence, end_sequence, summary_event.event_id, reason,
                 json.dumps(state, ensure_ascii=False), self._now()),
            )
            self._bump_context_version(session_id)
        return {"archive_id": archive_id, "summary_event_id": summary_event.event_id,
                "event_range": [start_sequence, end_sequence], "state": state}

    def latest_archive(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM archives WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def session_stats(self, session_id: str) -> Dict[str, int]:
        self.get_session(session_id)
        row = self._db.execute(
            """SELECT COUNT(*) AS count, COALESCE(SUM(token_estimate),0) AS tokens,
               COALESCE(MAX(sequence),0) AS latest FROM events WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        return dict(row)

    def close(self) -> None:
        self._db.close()
