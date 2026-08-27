from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, List

try:
    import jieba
except ImportError:  # pragma: no cover - fallback supports bootstrap before env creation
    jieba = None


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TECHNICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@-]+")


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def is_cjk(char: str) -> bool:
    return bool(CJK_RE.fullmatch(char))


def search_tokens(value: str) -> List[str]:
    normalized = normalize_text(value).lower()
    tokens: List[str] = []
    if jieba is not None:
        tokens.extend(token.strip() for token in jieba.cut(normalized, cut_all=False) if token.strip())
    cjk = [char for char in normalized if is_cjk(char)]
    tokens.extend(cjk)
    tokens.extend("".join(cjk[index:index + 2]) for index in range(len(cjk) - 1))
    tokens.extend(TECHNICAL_TOKEN_RE.findall(normalized))
    return list(dict.fromkeys(token for token in tokens if token and not token.isspace()))


def searchable_text(value: str) -> str:
    return " ".join(search_tokens(value))


def estimate_tokens(value: Any) -> int:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cjk_count = sum(1 for char in value if is_cjk(char))
    non_cjk_count = len(value) - cjk_count
    return max(1, cjk_count + (non_cjk_count + 3) // 4)


def excerpt(value: str, limit: int = 240) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"

