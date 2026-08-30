from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from zagent.context.retrieval import sparse_terms
from zagent.context.tokenization import normalize_text
from zagent.domain.errors import NotFoundError, ValidationError
from zagent.storage.sqlite_store import SqliteStore

BLOCKED_EVENT_KINDS = {"model_raw", "assistant_reasoning", "archive", "checkpoint"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S+", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class LongTermMemory:
    """Auditable long-term memory with explicit confirmation and scoped recall.

    Memory is deliberately not a transcript mirror.  Every item has source
    events, a stable key, a scope and a lifecycle.  Retrieval fuses SQLite FTS
    with a persisted deterministic sparse Chinese index; no training or remote
    embedding service is required.
    """

    def __init__(self, store: SqliteStore, *, rrf_k: int = 60) -> None:
        self._store = store
        self._rrf_k = rrf_k

    def scope_pairs(self, session_id: str) -> List[tuple[str, str]]:
        session = self._store.get_session(session_id)
        pairs = [("user", "local-user")]
        if session.get("workspace_id"):
            pairs.append(("workspace", str(session["workspace_id"])))
        return pairs

    def remember(
        self,
        session_id: str,
        *,
        memory_type: str,
        memory_key: str,
        content: str,
        source_event_ids: Sequence[str],
        reason: str,
        scope: str = "workspace",
        confidence: float = 0.8,
        confirmed: bool = False,
        pinned: bool = False,
        expires_at: str | None = None,
    ) -> Dict[str, Any]:
        if memory_type not in {"episodic", "semantic", "procedural"}:
            raise ValidationError("memory_type must be episodic, semantic, or procedural")
        if scope not in {"workspace", "user"}:
            raise ValidationError("memory scope must be workspace or user")
        clean_key = normalize_text(memory_key).casefold()
        clean_content = content.strip()
        if not clean_key or not clean_content:
            raise ValidationError("memory key and content must not be empty")
        if len(clean_key) > 200 or len(clean_content) > 8000:
            raise ValidationError("memory key or content is too long")
        if not source_event_ids:
            raise ValidationError("memory requires at least one source event")
        if any(pattern.search(clean_content) for pattern in SECRET_PATTERNS):
            raise ValidationError("memory content looks like a secret or credential")
        if expires_at is not None:
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError("expires_at must be ISO 8601") from exc
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValidationError("expires_at must be a future timezone-aware timestamp")

        unique_sources = list(dict.fromkeys(source_event_ids))
        for event_id in unique_sources:
            event = self._store.get_event(event_id)
            if event.session_id != session_id:
                raise ValidationError("memory source belongs to another session")
            if event.kind in BLOCKED_EVENT_KINDS or event.sensitivity == "internal":
                raise ValidationError(
                    "internal, reasoning, archive and checkpoint events cannot be remembered"
                )

        scope_id = self._scope_id(session_id, scope)
        existing = self._store.find_active_memory(scope, scope_id, memory_type, clean_key)
        digest = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
        if existing and existing["content_sha256"] == digest:
            reinforced = self._store.reinforce_memory(
                existing["memory_id"], unique_sources, confidence
            )
            return {
                "memory": reinforced,
                "outcome": "already_active",
                "conflict_memory_id": None,
            }

        # A conflicting value never silently overwrites an active fact.  It is
        # retained as a candidate until an explicit confirm call names the item
        # it supersedes.
        status = "active" if confirmed and existing is None else "candidate"
        terms = dict(sparse_terms(f"{clean_key} {clean_content}"))
        memory = self._store.create_memory(
            scope_type=scope,
            scope_id=scope_id,
            memory_type=memory_type,
            memory_key=clean_key,
            content=clean_content,
            confidence=confidence,
            status=status,
            pinned=pinned,
            created_reason=reason,
            source_session_id=session_id,
            source_event_ids=unique_sources,
            expires_at=expires_at,
            terms=terms,
        )
        return {
            "memory": memory,
            "outcome": "active" if status == "active" else "candidate",
            "conflict_memory_id": existing["memory_id"] if existing else None,
        }

    def confirm(
        self, session_id: str, memory_id: str, supersedes_memory_id: str | None = None
    ) -> Dict[str, Any]:
        memory = self._store.get_memory(memory_id)
        self._assert_visible(session_id, memory)
        if supersedes_memory_id:
            previous = self._store.get_memory(supersedes_memory_id)
            self._assert_visible(session_id, previous)
        return {"memory": self._store.activate_memory(memory_id, supersedes_memory_id=supersedes_memory_id)}

    def forget(self, session_id: str, memory_id: str, reason: str) -> Dict[str, Any]:
        memory = self._store.get_memory(memory_id)
        self._assert_visible(session_id, memory)
        deleted = self._store.forget_memory(memory_id, reason)
        return {
            "memory_id": memory_id,
            "status": deleted["status"],
            "content_sha256": deleted["content_sha256"],
            "tombstone": True,
        }

    def list(
        self, session_id: str, *, include_candidates: bool = False, limit: int = 100
    ) -> List[Dict[str, Any]]:
        statuses = ("active", "candidate") if include_candidates else ("active",)
        return self._store.list_memories(self.scope_pairs(session_id), statuses=statuses, limit=limit)

    def search(self, session_id: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        clean_query = normalize_text(query).casefold()
        if not clean_query:
            return []
        scopes = self.scope_pairs(session_id)
        lexical = self._store.search_memory_lexical(scopes, clean_query, max(20, limit * 4))
        sparse = self._store.search_memory_terms(scopes, dict(sparse_terms(clean_query)), max(20, limit * 4))
        lexical_rank = {item["memory"]["memory_id"]: rank for rank, item in enumerate(lexical, start=1)}
        sparse_rank = {item["memory"]["memory_id"]: rank for rank, item in enumerate(sparse, start=1)}
        by_id = {item["memory"]["memory_id"]: item["memory"] for item in [*lexical, *sparse]}
        results = []
        for memory_id, memory in by_id.items():
            exact = clean_query in normalize_text(f"{memory['memory_key']} {memory['content']}").casefold()
            l_rank = lexical_rank.get(memory_id)
            s_rank = sparse_rank.get(memory_id)
            score = (1 / (self._rrf_k + l_rank) if l_rank else 0.0) + (
                0.85 / (self._rrf_k + s_rank) if s_rank else 0.0
            )
            channels = (
                (["exact"] if exact else [])
                + (["lexical"] if l_rank else [])
                + (["sparse"] if s_rank else [])
            )
            results.append(
                {
                    "memory": memory,
                    "channels": channels,
                    "exact_match": exact,
                    "fusion_score": round(score, 6),
                    "lexical_rank": l_rank,
                    "sparse_rank": s_rank,
                }
            )
        return sorted(
            results,
            key=lambda item: (
                not item["exact_match"],
                -item["fusion_score"],
                -float(item["memory"]["confidence"]),
                -int(item["memory"]["pinned"]),
            ),
        )[:limit]

    def prompt_memories(self, session_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        pinned = self._store.list_memories(self.scope_pairs(session_id), pinned_only=True, limit=limit)
        selected = {item["memory_id"]: item for item in pinned}
        for result in self.search(session_id, query, limit=limit):
            memory = result["memory"]
            selected.setdefault(memory["memory_id"], memory)
            if len(selected) >= limit:
                break
        return list(selected.values())[:limit]

    def _scope_id(self, session_id: str, scope: str) -> str:
        if scope == "user":
            return "local-user"
        session = self._store.get_session(session_id)
        workspace_id = session.get("workspace_id")
        if not workspace_id:
            raise ValidationError("session has no workspace for workspace memory")
        return str(workspace_id)

    def _assert_visible(self, session_id: str, memory: Dict[str, Any]) -> None:
        if (memory["scope_type"], memory["scope_id"]) not in set(self.scope_pairs(session_id)):
            raise NotFoundError("memory not found in this scope")
