from __future__ import annotations

from typing import Dict, List, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextStatusArgs(StrictArgs):
    pass


class ContextSearchArgs(StrictArgs):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=20)


class ContextRetrieveArgs(StrictArgs):
    event_ids: List[str] = Field(min_length=1, max_length=50)
    max_chars: int = Field(default=20_000, ge=256, le=100_000)


class ArchiveState(StrictArgs):
    goal: str
    completed: List[str]
    decisions: List[str]
    risks: List[str]
    next_steps: List[str]


class ContextArchiveArgs(StrictArgs):
    reason: str = Field(min_length=1, max_length=2000)
    start_sequence: int = Field(ge=1)
    end_sequence: int = Field(ge=1)
    state_update: ArchiveState


class ContextPinArgs(StrictArgs):
    event_ids: List[str] = Field(min_length=1, max_length=50)
    rationale: str = Field(min_length=1, max_length=2000)


class ContextUnpinArgs(StrictArgs):
    event_ids: List[str] = Field(min_length=1, max_length=50)


class MemoryRememberArgs(StrictArgs):
    memory_type: Literal["episodic", "semantic", "procedural"]
    memory_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=8000)
    source_event_ids: List[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)
    scope: Literal["workspace", "user"] = "workspace"
    confidence: float = Field(default=0.8, ge=0, le=1)
    confirmed: bool = False
    pinned: bool = False
    expires_at: Optional[str] = None


class MemoryConfirmArgs(StrictArgs):
    memory_id: str = Field(min_length=5, max_length=80)
    supersedes_memory_id: Optional[str] = Field(default=None, min_length=5, max_length=80)


class MemorySearchArgs(StrictArgs):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=20)


class MemoryListArgs(StrictArgs):
    include_candidates: bool = False
    limit: int = Field(default=50, ge=1, le=100)


class MemoryForgetArgs(StrictArgs):
    memory_id: str = Field(min_length=5, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


CONTEXT_ARGUMENT_TYPES: Dict[str, Type[StrictArgs]] = {
    "context_status": ContextStatusArgs,
    "context_search": ContextSearchArgs,
    "context_retrieve": ContextRetrieveArgs,
    "context_archive": ContextArchiveArgs,
    "context_pin": ContextPinArgs,
    "context_unpin": ContextUnpinArgs,
    "memory_remember": MemoryRememberArgs,
    "memory_confirm": MemoryConfirmArgs,
    "memory_search": MemorySearchArgs,
    "memory_list": MemoryListArgs,
    "memory_forget": MemoryForgetArgs,
}

CONTEXT_TOOL_DESCRIPTIONS = {
    "context_status": "查看当前上下文预算、事件和归档状态",
    "context_search": "用中文 BM25 与本地稀疏向量混合搜索历史原文，返回稳定 event_id",
    "context_retrieve": "通过稳定 event_id 分页取回原文",
    "context_archive": "结束一个已完成阶段，归档事件并更新结构化任务状态",
    "context_pin": "把关键证据固定到工作上下文",
    "context_unpin": "取消固定证据",
    "memory_remember": (
        "将有 event_id 来源的稳定事实、任务经历或用户流程偏好写入长期记忆。"
        "仅当用户明确要求记住时 confirmed 才可为 true；否则只创建候选"
    ),
    "memory_confirm": "确认候选长期记忆；冲突更新必须显式给出被替代的 memory_id",
    "memory_search": "跨当前工作区会话检索已确认的长期记忆，返回来源 event_id 与召回通道",
    "memory_list": "列出当前用户和工作区作用域内的长期记忆",
    "memory_forget": "删除长期记忆正文与检索索引，保留不含正文的审计 tombstone",
}


def context_tool_schemas() -> List[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": CONTEXT_TOOL_DESCRIPTIONS[name],
                "parameters": argument_type.model_json_schema(),
            },
        }
        for name, argument_type in CONTEXT_ARGUMENT_TYPES.items()
    ]
