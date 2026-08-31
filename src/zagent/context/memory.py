from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from zagent.context.retrieval import sparse_terms
from zagent.context.tokenization import normalize_text
from zagent.domain.errors import ConcurrentUpdateError, NotFoundError, ValidationError
from zagent.storage.sqlite_store import SqliteStore

BLOCKED_EVENT_KINDS = {"model_raw", "assistant_reasoning", "archive", "checkpoint"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S+", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
REMEMBER_INTENT_RE = re.compile(
    r"(?:请|帮我)?(?:记住|记下来|保存.{0,6}(?:记忆|偏好)|更新.{0,6}记忆)"
    r"|(?:以后|今后)(?:请|都|总是|一律|默认|不要|别).{1,30}"
    r"|我的偏好是.{1,50}"
    r"|\b(?:remember|save (?:this|that).{0,20}|update.{0,12}memory)\b",
    re.I,
)
FORGET_INTENT_RE = re.compile(
    r"(?:忘掉|忘记|删除|清除|移除).{0,12}(?:记忆|偏好|这条|该条)"
    r"|\b(?:forget|delete|remove|clear).{0,20}(?:memory|preference|this|that)\b",
    re.I,
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
        user_action: bool = False,
    ) -> Dict[str, Any]:
        if memory_type not in {"episodic", "semantic", "procedural"}:
            raise ValidationError("memory_type must be episodic, semantic, or procedural")
        if scope not in {"workspace", "user"}:
            raise ValidationError("memory scope must be workspace or user")
        if not 0 <= confidence <= 1:
            raise ValidationError("memory confidence must be between 0 and 1")
        if pinned and not user_action:
            raise ValidationError("pinning memory requires a direct user action")
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
        explicit_user_intent = False
        for event_id in unique_sources:
            event = self._store.get_event(event_id)
            if event.session_id != session_id:
                raise ValidationError("memory source belongs to another session")
            if event.kind in BLOCKED_EVENT_KINDS or event.sensitivity == "internal":
                raise ValidationError(
                    "internal, reasoning, archive and checkpoint events cannot be remembered"
                )
            if (
                event.role == "user"
                and isinstance(event.payload, str)
                and REMEMBER_INTENT_RE.search(normalize_text(event.payload))
            ):
                explicit_user_intent = True
        if confirmed and not (user_action or explicit_user_intent):
            raise ValidationError(
                "confirmed memory requires an explicit user remember request; create a candidate instead"
            )

        scope_id = self._scope_id(session_id, scope)
        self._store.expire_memories([(scope, scope_id)])
        terms = dict(sparse_terms(f"{clean_key} {clean_content}"))
        return self._store.create_memory(
            scope_type=scope,
            scope_id=scope_id,
            memory_type=memory_type,
            memory_key=clean_key,
            content=clean_content,
            confidence=confidence,
            confirmed=confirmed,
            pinned=pinned,
            created_reason=reason,
            source_session_id=session_id,
            source_event_ids=unique_sources,
            expires_at=expires_at,
            terms=terms,
        )

    def confirm(
        self,
        session_id: str,
        memory_id: str,
        supersedes_memory_id: str | None = None,
        confirmation_event_id: str | None = None,
        *,
        user_action: bool = False,
    ) -> Dict[str, Any]:
        self._store.expire_memories(self.scope_pairs(session_id))
        memory = self._store.get_memory(memory_id)
        self._assert_visible(session_id, memory)
        if not user_action:
            self._validate_confirmation_event(session_id, confirmation_event_id, REMEMBER_INTENT_RE)
        if supersedes_memory_id:
            previous = self._store.get_memory(supersedes_memory_id)
            self._assert_visible(session_id, previous)
        return {"memory": self._store.activate_memory(memory_id, supersedes_memory_id=supersedes_memory_id)}

    def forget(
        self,
        session_id: str,
        memory_id: str,
        reason: str,
        confirmation_event_id: str | None = None,
        *,
        user_action: bool = False,
    ) -> Dict[str, Any]:
        memory = self._store.get_memory(memory_id)
        self._assert_visible(session_id, memory)
        if not user_action:
            self._validate_confirmation_event(session_id, confirmation_event_id, FORGET_INTENT_RE)
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
        self._store.expire_memories(self.scope_pairs(session_id))
        statuses = ("active", "candidate") if include_candidates else ("active",)
        memories = self._store.list_memories(self.scope_pairs(session_id), statuses=statuses, limit=limit)
        if include_candidates:
            for memory in memories:
                if memory["status"] != "candidate":
                    continue
                conflict = self._store.find_active_memory(
                    memory["scope_type"],
                    memory["scope_id"],
                    memory["memory_type"],
                    memory["memory_key"],
                )
                memory["conflict_memory_id"] = conflict["memory_id"] if conflict else None
        return memories

    def search(self, session_id: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        clean_query = normalize_text(query).casefold()
        if not clean_query:
            return []
        scopes = self.scope_pairs(session_id)
        self._store.expire_memories(scopes)
        lexical = self._store.search_memory_lexical(scopes, clean_query, max(20, limit * 4))
        sparse = self._store.search_memory_terms(scopes, dict(sparse_terms(clean_query)), max(20, limit * 4))
        lexical_rank = {item["memory"]["memory_id"]: rank for rank, item in enumerate(lexical, start=1)}
        sparse_rank = {item["memory"]["memory_id"]: rank for rank, item in enumerate(sparse, start=1)}
        lexical_by_id = {item["memory"]["memory_id"]: item for item in lexical}
        sparse_by_id = {item["memory"]["memory_id"]: item for item in sparse}
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
                    "lexical_score": lexical_by_id[memory_id]["score"] if l_rank else None,
                    "sparse_score": sparse_by_id[memory_id]["score"] if s_rank else None,
                    "sparse_query_coverage": (sparse_by_id[memory_id]["query_coverage"] if s_rank else None),
                    "matched_terms": sparse_by_id[memory_id]["matched_terms"] if s_rank else [],
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

    def audit(self, session_id: str, memory_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        memory = self._store.get_memory(memory_id)
        self._assert_visible(session_id, memory)
        return self._store.list_memory_audit(memory_id, limit)

    def export(self, session_id: str) -> Dict[str, Any]:
        session = self._store.get_session(session_id)
        memories = self.list(session_id, include_candidates=True, limit=500)
        return {
            "schema_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "workspace_id": session.get("workspace_id"),
            "memories": [
                {
                    **memory,
                    "audit": self._store.list_memory_audit(memory["memory_id"], 500),
                }
                for memory in memories
            ],
        }

    def set_pinned(
        self,
        session_id: str,
        memory_id: str,
        pinned: bool,
        *,
        expected_pinned: bool,
    ) -> Dict[str, Any]:
        self._store.expire_memories(self.scope_pairs(session_id))
        memory = self._store.get_memory(memory_id)
        self._assert_visible(session_id, memory)
        return self._store.set_memory_pinned(
            memory_id,
            pinned,
            expected_pinned=expected_pinned,
        )

    def correct(
        self,
        session_id: str,
        memory_id: str,
        content: str,
        reason: str,
    ) -> Dict[str, Any]:
        self._store.expire_memories(self.scope_pairs(session_id))
        current = self._store.get_memory(memory_id)
        self._assert_visible(session_id, current)
        if current["status"] != "active":
            raise ValidationError("only active memory can be corrected")
        clean_content = content.strip()
        if not clean_content or len(clean_content) > 8000:
            raise ValidationError("corrected memory content is empty or too long")
        if any(pattern.search(clean_content) for pattern in SECRET_PATTERNS):
            raise ValidationError("memory content looks like a secret or credential")
        if normalize_text(clean_content) == normalize_text(current["content"]):
            raise ValidationError("corrected memory content is unchanged")
        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 1000:
            raise ValidationError("memory correction reason is empty or too long")

        evidence = self._store.append_event(
            session_id,
            "memory_correction",
            "user",
            {
                "memory_id": memory_id,
                "memory_key": current["memory_key"],
                "content": clean_content,
                "reason": clean_reason,
            },
            provenance="user",
        )
        candidate = self.remember(
            session_id,
            memory_type=current["memory_type"],
            memory_key=current["memory_key"],
            content=clean_content,
            source_event_ids=[evidence.event_id],
            reason=clean_reason,
            scope=current["scope_type"],
            confidence=1.0,
            confirmed=True,
            pinned=current["pinned"],
            expires_at=current["expires_at"],
            user_action=True,
        )
        replacement = candidate["memory"]
        if candidate["outcome"] != "candidate":
            raise ConcurrentUpdateError("memory changed while correction was being prepared")
        activated = self._store.activate_memory(
            replacement["memory_id"],
            supersedes_memory_id=memory_id,
        )
        return {
            "memory": activated,
            "superseded_memory_id": memory_id,
            "evidence_event_id": evidence.event_id,
        }

    def prompt_memories(self, session_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        pinned = self._store.list_memories(self.scope_pairs(session_id), pinned_only=True, limit=limit)
        selected = {item["memory_id"]: item for item in pinned}
        for result in self.search(session_id, query, limit=limit):
            if not result["exact_match"] and not (
                result["lexical_rank"]
                and result["sparse_rank"]
                and float(result["sparse_query_coverage"] or 0) >= 0.12
                and float(result["sparse_score"] or 0) >= 2.0
                and (int(result["sparse_rank"]) == 1 or float(result["sparse_score"] or 0) >= 8.0)
            ):
                continue
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

    def _validate_confirmation_event(
        self, session_id: str, event_id: str | None, intent: re.Pattern[str]
    ) -> None:
        if not event_id:
            raise ValidationError("memory mutation requires a user confirmation event_id")
        event = self._store.get_event(event_id)
        if (
            event.session_id != session_id
            or event.role != "user"
            or not isinstance(event.payload, str)
            or not intent.search(normalize_text(event.payload))
        ):
            raise ValidationError("memory confirmation event is not an explicit user request")
