from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, List

try:
    import jieba
except ImportError:  # pragma: no cover - fallback supports bootstrap before env creation
    jieba = None

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover - fallback keeps the Core bootable during migration
    OpenCC = None  # type: ignore[assignment,misc]


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
TECHNICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@-]+")

_OPENCC = OpenCC("t2s") if OpenCC is not None else None

# Small, explicit domain aliases complement character n-grams.  They are not a
# semantic model: every expansion remains inspectable and deterministic.
ZH_ALIAS_GROUPS = (
    frozenset({"项目", "专案"}),
    frozenset({"登录", "登入"}),
    frozenset({"认证", "鉴权", "身份验证", "身份校验"}),
    frozenset({"配置", "设置", "设定"}),
    frozenset({"缓存", "快取"}),
    frozenset({"数据库", "资料库"}),
    frozenset({"工作区", "工作空间"}),
    frozenset({"报错", "错误", "异常"}),
    frozenset({"依赖", "套件", "软件包"}),
)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return _OPENCC.convert(normalized) if _OPENCC is not None else normalized


def is_cjk(char: str) -> bool:
    return bool(CJK_RE.fullmatch(char))


def search_tokens(value: str) -> List[str]:
    normalized = normalize_text(value).lower()
    tokens: List[str] = []
    if jieba is not None:
        tokens.extend(token.strip() for token in jieba.cut(normalized, cut_all=False) if token.strip())
    for run in CJK_RUN_RE.findall(normalized):
        tokens.extend(run)
        tokens.append(run)
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    tokens.extend(TECHNICAL_TOKEN_RE.findall(normalized))
    present = set(tokens)
    for group in ZH_ALIAS_GROUPS:
        if group & present or any(alias in normalized for alias in group):
            tokens.extend(sorted(group))
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
