from zagent.context.retrieval import HybridRetriever, retrieval_channels, sparse_terms


def test_sparse_terms_normalize_chinese_and_technical_identifiers():
    terms = sparse_terms("调用 CONTEXT_SEARCH，读取 src/zagent/context.py")
    assert "t:context_search" in terms
    assert "g2:调用" in terms
    assert "t:src/zagent/context.py" in terms


def test_vector_channel_recovers_chinese_phrase_variant_when_lexical_is_empty(
    store, session_id, monkeypatch
):
    target = store.append_event(
        session_id, "message", "assistant", "用户登录失败，需要检查认证服务"
    )
    store.append_event(session_id, "message", "assistant", "数据库迁移已经完成")
    store.append_event(session_id, "message", "assistant", "身份校验模块发生故障")
    monkeypatch.setattr(store, "search_events", lambda *_args, **_kwargs: [])

    results = HybridRetriever(store).search(session_id, "登录认证故障", 5)
    assert results[0]["event"]["event_id"] == target.event_id
    assert results[0]["channels"] == ["vector"]
    assert results[0]["vector_similarity"] >= 0.08
    assert results[0]["score"] is None  # no lexical/BM25 score was fabricated


def test_exact_phrase_guard_beats_fused_partial_matches(store, session_id):
    exact = store.append_event(session_id, "message", "assistant", "数据库迁移已完成")
    for index in range(8):
        store.append_event(
            session_id,
            "message",
            "assistant",
            f"数据库检查、迁移校验与缓存迁移过程 {index}",
        )

    results = HybridRetriever(store).search(session_id, "数据库迁移", 5)
    assert results[0]["event"]["event_id"] == exact.event_id
    assert results[0]["exact_match"] is True
    assert results[0]["channels"][0] == "exact"
    assert {"lexical", "vector"} <= retrieval_channels(results)


def test_hybrid_search_never_vectors_internal_events(store, session_id, monkeypatch):
    public = store.append_event(session_id, "message", "assistant", "公开的认证故障信息")
    store.append_event(
        session_id,
        "model_raw",
        "system",
        "内部的认证故障密钥",
        sensitivity="internal",
    )
    monkeypatch.setattr(store, "search_events", lambda *_args, **_kwargs: [])

    results = HybridRetriever(store).search(session_id, "认证故障", 10)
    assert [item["event"]["event_id"] for item in results] == [public.event_id]
    assert "密钥" not in str(results)
