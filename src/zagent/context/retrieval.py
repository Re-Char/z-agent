from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Sequence

from zagent.context.tokenization import (
    TECHNICAL_TOKEN_RE,
    excerpt,
    normalize_text,
    search_tokens,
)
from zagent.domain.models import EventRecord
from zagent.storage.sqlite_store import SqliteStore

CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

# High-frequency conversational words should help recall a little, but must
# not outrank the actual subject (for example "部署" versus "项目").  The
# list is intentionally small and inspectable instead of being learned from
# user data.
LOW_INFORMATION_CJK_TERMS = frozenset(
    {
        "一下",
        "什么",
        "使用",
        "哪个",
        "哪個",
        "如何",
        "帮我",
        "幫我",
        "怎么",
        "怎样",
        "是否",
        "这个",
        "那个",
        "项目",
        "專案",
        "专案",
    }
)


def _event_text(event: EventRecord) -> str:
    if isinstance(event.payload, str):
        return event.payload
    return json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))


def sparse_terms(value: str) -> Counter[str]:
    """Create a deterministic Chinese-first sparse vector without model training.

    The vector combines normalized tokenizer output, CJK 3-grams and technical
    identifier components.  Single CJK characters receive a low weight so they
    improve recall without overwhelming exact phrases and identifiers.
    """
    normalized = normalize_text(value).casefold()
    terms: Counter[str] = Counter()
    for token in search_tokens(normalized):
        if len(token) == 1 and CJK_RUN_RE.fullmatch(token):
            terms[f"c:{token}"] += 0.2
        elif TECHNICAL_TOKEN_RE.fullmatch(token):
            terms[f"t:{token}"] += 1.5
            for component in re.split(r"[\s_./:@-]+", token):
                if len(component) >= 2:
                    terms[f"p:{component}"] += 0.7
        else:
            terms[f"w:{token}"] += 0.15 if token in LOW_INFORMATION_CJK_TERMS else 1.0
    for run in CJK_RUN_RE.findall(normalized):
        for size, weight in ((2, 1.0), (3, 1.25)):
            for index in range(max(0, len(run) - size + 1)):
                gram = run[index : index + size]
                terms[f"g{size}:{gram}"] += 0.15 if gram in LOW_INFORMATION_CJK_TERMS else weight
    return terms


def _tfidf_vectors(
    query: Counter[str], documents: Sequence[Counter[str]]
) -> tuple[Dict[str, float], List[Dict[str, float]]]:
    document_count = len(documents)
    frequencies: Counter[str] = Counter()
    for document in documents:
        frequencies.update(document.keys())

    def transform(source: Counter[str], *, ignore_unseen: bool = False) -> Dict[str, float]:
        vector: Dict[str, float] = {}
        for term, count in source.items():
            if ignore_unseen and frequencies[term] == 0:
                continue
            inverse_frequency = math.log((document_count + 1) / (frequencies[term] + 1)) + 1
            vector[term] = float(count) * inverse_frequency
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        if norm:
            vector = {term: weight / norm for term, weight in vector.items()}
        return vector

    return transform(query, ignore_unseen=True), [transform(document) for document in documents]


def _cosine(left: Dict[str, float], right: Dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(term, 0.0) for term, weight in left.items())


class HybridRetriever:
    """Fuse SQLite BM25 with an in-process sparse TF-IDF vector channel.

    This v1 implementation deliberately has no persistent vector index.  It
    computes vectors for a bounded recent corpus per query and falls back to
    the full-history FTS5 channel for exact historical evidence.
    """

    def __init__(
        self,
        store: SqliteStore,
        *,
        vector_corpus_limit: int = 1000,
        vector_min_similarity: float = 0.04,
        rrf_k: int = 60,
    ) -> None:
        self._store = store
        self._vector_corpus_limit = vector_corpus_limit
        self._vector_min_similarity = vector_min_similarity
        self._rrf_k = rrf_k

    def search(self, session_id: str, query: str, limit: int = 10) -> List[dict]:
        lexical = self._store.search_events(session_id, query, max(limit * 4, 20))
        corpus = self._store.list_searchable_events(session_id, self._vector_corpus_limit)
        vector = self._vector_rank(query, corpus, max(limit * 4, 20))
        return self._fuse(query, lexical, vector, limit)

    def _vector_rank(
        self, query: str, events: Sequence[EventRecord], limit: int
    ) -> List[tuple[EventRecord, float]]:
        if not events:
            return []
        query_terms = sparse_terms(query)
        if not query_terms:
            return []
        document_terms = [sparse_terms(_event_text(event)) for event in events]
        query_vector, document_vectors = _tfidf_vectors(query_terms, document_terms)
        ranked = [
            (event, _cosine(query_vector, vector))
            for event, vector in zip(events, document_vectors, strict=True)
        ]
        return sorted(
            (item for item in ranked if item[1] >= self._vector_min_similarity),
            key=lambda item: (-item[1], -item[0].sequence),
        )[:limit]

    def _fuse(
        self,
        query: str,
        lexical: Sequence[dict],
        vector: Sequence[tuple[EventRecord, float]],
        limit: int,
    ) -> List[dict]:
        lexical_by_id = {item["event"]["event_id"]: item for item in lexical}
        lexical_ranks = {item["event"]["event_id"]: rank for rank, item in enumerate(lexical, start=1)}
        vector_by_id = {event.event_id: (event, similarity) for event, similarity in vector}
        vector_ranks = {event.event_id: rank for rank, (event, _) in enumerate(vector, start=1)}
        event_ids = list(dict.fromkeys([*lexical_ranks, *vector_ranks]))
        normalized_query = normalize_text(query).casefold()
        results: List[dict] = []
        for event_id in event_ids:
            lexical_item = lexical_by_id.get(event_id)
            vector_item = vector_by_id.get(event_id)
            event = vector_item[0] if vector_item else self._event_from_lexical(lexical_item)
            text = _event_text(event)
            exact = bool(normalized_query and normalized_query in normalize_text(text).casefold())
            lexical_rank = lexical_ranks.get(event_id)
            vector_rank = vector_ranks.get(event_id)
            fusion_score = 0.0
            channels: List[str] = []
            if lexical_rank is not None:
                fusion_score += 1.0 / (self._rrf_k + lexical_rank)
                channels.append("lexical")
            if vector_rank is not None:
                fusion_score += 0.85 / (self._rrf_k + vector_rank)
                channels.append("vector")
            if exact:
                channels.insert(0, "exact")
            results.append(
                {
                    "event": event.to_dict(),
                    "excerpt": lexical_item["excerpt"] if lexical_item else excerpt(text),
                    "score": lexical_item["score"] if lexical_item else None,
                    "fusion_score": round(fusion_score, 6),
                    "channels": channels,
                    "exact_match": exact,
                    "lexical_rank": lexical_rank,
                    "vector_rank": vector_rank,
                    "vector_similarity": round(vector_item[1], 4) if vector_item else None,
                }
            )
        return sorted(
            results,
            key=lambda item: (
                not item["exact_match"],
                -item["fusion_score"],
                item["lexical_rank"] if item["lexical_rank"] is not None else 10**9,
                -item["event"]["sequence"],
            ),
        )[:limit]

    @staticmethod
    def _event_from_lexical(item: dict | None) -> EventRecord:
        if item is None:  # pragma: no cover - union construction guarantees an item
            raise ValueError("missing retrieval event")
        return EventRecord(**item["event"])


def retrieval_channels(results: Iterable[dict]) -> set[str]:
    """Small public helper for diagnostics and tests."""
    return {channel for result in results for channel in result.get("channels", [])}
