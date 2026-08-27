from zagent.context.tokenization import estimate_tokens, normalize_text, search_tokens, searchable_text


def test_normalize_full_width_characters():
    assert normalize_text("ＡＰＩ１２３") == "API123"


def test_search_tokens_keep_chinese_and_technical_identifiers():
    tokens = search_tokens("调用 context_search 搜索项目状态")
    assert "context_search" in tokens
    assert "项目" in tokens or "项目状态" in tokens
    assert "项" in tokens


def test_searchable_text_is_deterministic():
    assert searchable_text("中文 API") == searchable_text("中文 API")


def test_token_estimation_counts_chinese_conservatively():
    assert estimate_tokens("中文上下文") >= 5
    assert estimate_tokens("abcd") == 1

