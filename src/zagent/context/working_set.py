from __future__ import annotations

import json
from typing import Any, Dict, List

from zagent.domain.models import EventRecord, WorkingSet
from zagent.storage.sqlite_store import SqliteStore

from .tokenization import estimate_tokens

SYSTEM_PROMPT_ZH = """你是 Z-Agent，一个中文优先、可审计的长程任务智能体。
你可以使用上下文工具主动管理工作记忆：阶段完成后调用 context_archive；需要旧细节时先用
context_search，再用 context_retrieve 获取原文。归档摘要不是事实源，关键结论必须引用 event_id。
工具参数、代码、命令、路径和 JSON key 必须保持原样。工具信息不足时应追问或检索，不得虚构结果。"""


class WorkingSetBuilder:
    def __init__(
        self,
        store: SqliteStore,
        *,
        context_window: int = 32_768,
        hard_limit_ratio: float = 0.82,
        recent_event_limit: int = 24,
    ) -> None:
        self._store = store
        self._budget = int(context_window * hard_limit_ratio)
        self._recent_event_limit = recent_event_limit

    @property
    def budget(self) -> int:
        return self._budget

    def build(self, session_id: str) -> WorkingSet:
        recent = [
            event for event in self._store.recent_events(session_id, self._recent_event_limit * 2)
            if event.kind != "model_raw"
        ][-self._recent_event_limit:]
        pinned = self._store.pinned_events(session_id)
        unique_events = {event.event_id: event for event in pinned + recent}
        ordered = sorted(unique_events.values(), key=lambda event: event.sequence)
        pinned_ids = {event.event_id for event in pinned}

        system_prompt = self._system_prompt(session_id)
        used_tokens = estimate_tokens(system_prompt)
        selected: List[EventRecord] = []
        for event in reversed(ordered):
            event_cost = event.token_estimate + 8
            if used_tokens + event_cost > self._budget and event.event_id not in pinned_ids:
                continue
            selected.append(event)
            used_tokens += event_cost
        selected.reverse()
        return WorkingSet(
            messages=[{"role": "system", "content": system_prompt}]
            + [self._to_message(event) for event in selected],
            token_estimate=used_tokens,
            budget=self._budget,
            included_event_ids=[event.event_id for event in selected],
            pinned_event_ids=sorted(pinned_ids),
        )

    def _system_prompt(self, session_id: str) -> str:
        prompt = SYSTEM_PROMPT_ZH
        archive = self._store.latest_archive(session_id)
        if archive:
            prompt += "\n\n当前任务状态：\n" + json.dumps(archive["state"], ensure_ascii=False)
            prompt += f"\n最近归档：{archive['archive_id']}，可用 context_search/context_retrieve 展开。"
        return prompt

    @staticmethod
    def _to_message(event: EventRecord) -> Dict[str, Any]:
        if event.kind == "assistant_tool_calls" and isinstance(event.payload, dict):
            calls = []
            for call in event.payload.get("tool_calls", []):
                calls.append({
                    "id": call["call_id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    },
                })
            return {"role": "assistant", "content": event.payload.get("content") or None, "tool_calls": calls}
        content = (
            event.payload
            if isinstance(event.payload, str)
            else json.dumps(event.payload, ensure_ascii=False)
        )
        message: Dict[str, Any] = {
            "role": event.role if event.role in {"user", "assistant", "tool", "system"} else "system",
            "content": content,
        }
        if event.role == "tool" and event.tool_call_id:
            message["tool_call_id"] = event.tool_call_id
        return message
