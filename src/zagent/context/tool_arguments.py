from __future__ import annotations

from typing import Dict, List, Type

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


CONTEXT_ARGUMENT_TYPES: Dict[str, Type[StrictArgs]] = {
    "context_status": ContextStatusArgs,
    "context_search": ContextSearchArgs,
    "context_retrieve": ContextRetrieveArgs,
    "context_archive": ContextArchiveArgs,
    "context_pin": ContextPinArgs,
    "context_unpin": ContextUnpinArgs,
}

CONTEXT_TOOL_DESCRIPTIONS = {
    "context_status": "查看当前上下文预算、事件和归档状态",
    "context_search": "用中文 BM25 与本地稀疏向量混合搜索历史原文，返回稳定 event_id",
    "context_retrieve": "通过稳定 event_id 分页取回原文",
    "context_archive": "结束一个已完成阶段，归档事件并更新结构化任务状态",
    "context_pin": "把关键证据固定到工作上下文",
    "context_unpin": "取消固定证据",
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
