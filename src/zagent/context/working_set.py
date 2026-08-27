from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

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
        self._context_window = context_window
        self._budget = int(context_window * hard_limit_ratio)
        # Hard ceiling for the whole working set (system + pinned + recent).
        # Pinned evidence may use the budget-to-window headroom, but can never
        # exceed the provider's real context window.
        self._hard_cap = context_window
        self._recent_event_limit = recent_event_limit
        # Per-session build cache keyed by the store's monotonic context version.
        self._cache: Dict[str, Tuple[int, WorkingSet]] = {}

    @property
    def budget(self) -> int:
        return self._budget

    def build(self, session_id: str) -> WorkingSet:
        version = self._store.context_version(session_id)
        cached = self._cache.get(session_id)
        if cached is not None and cached[0] == version:
            return cached[1]
        working = self._build(session_id)
        self._cache[session_id] = (version, working)
        return working

    def _build(self, session_id: str) -> WorkingSet:
        recent = [
            event for event in self._store.recent_events(session_id, self._recent_event_limit * 2)
            if event.kind not in {"model_raw", "archive"}
        ][-self._recent_event_limit:]
        pinned = self._store.pinned_events(session_id)
        unique_events = {event.event_id: event for event in pinned + recent}
        ordered = sorted(unique_events.values(), key=lambda event: event.sequence)
        pinned_ids = {event.event_id for event in pinned}

        system_prompt = self._system_prompt(session_id)
        used_tokens = estimate_tokens(system_prompt)
        selected: List[EventRecord] = []
        dropped_pinned: List[str] = []
        pinned_tokens = 0
        for event in reversed(ordered):
            # Archive summaries must never appear mid-conversation: the current
            # archive state is already injected into the system prompt, and an
            # intermediate "system" message between an assistant tool_calls
            # message and its tool replies breaks OpenAI-compatible providers
            # (DeepSeek rejects it with HTTP 400).
            if event.kind == "archive":
                continue
            event_cost = event.token_estimate + 8
            is_pinned = event.event_id in pinned_ids
            if used_tokens + event_cost > self._hard_cap:
                # Hard safety limit: even pinned evidence must never exceed the
                # provider's context_window, otherwise the request fails with
                # HTTP 400 and the whole task dies.
                if is_pinned:
                    dropped_pinned.append(event.event_id)
                continue
            if not is_pinned and used_tokens + event_cost > self._budget:
                continue
            selected.append(event)
            used_tokens += event_cost
            if is_pinned:
                pinned_tokens += event_cost
        selected.reverse()
        # Tool rounds must stay intact: an assistant tool_calls message needs
        # every one of its tool replies, and a tool reply needs its caller.
        # The recent-window / budget truncation above can split a round apart,
        # which OpenAI-compatible providers reject with HTTP 400.
        selected = self._drop_broken_tool_rounds(selected)
        return WorkingSet(
            messages=[{"role": "system", "content": system_prompt}]
            + [self._to_message(event) for event in selected],
            token_estimate=used_tokens,
            budget=self._budget,
            included_event_ids=[event.event_id for event in selected],
            pinned_event_ids=sorted(pinned_ids),
            dropped_pinned_ids=sorted(dropped_pinned),
            pinned_tokens=pinned_tokens,
        )

    @staticmethod
    def _drop_broken_tool_rounds(events: List[EventRecord]) -> List[EventRecord]:
        """Keep only tool rounds that are fully intact within the truncated window.

        A round is a complete assistant tool_calls message plus one tool reply
        per call.  Anything truncated mid-round is dropped as a whole so the
        message sequence stays valid for OpenAI-compatible providers.
        """
        caller_calls: Dict[str, set] = {}
        call_to_caller: Dict[str, str] = {}
        for event in events:
            if event.kind == "assistant_tool_calls" and isinstance(event.payload, dict):
                call_ids = {call.get("call_id") for call in event.payload.get("tool_calls", [])}
                caller_calls[event.event_id] = call_ids
                for call_id in call_ids:
                    call_to_caller[call_id] = event.event_id
        replied: Dict[str, set] = {}
        for event in events:
            if event.kind == "tool_result" and event.tool_call_id in call_to_caller:
                caller = call_to_caller[event.tool_call_id]
                replied.setdefault(caller, set()).add(event.tool_call_id)
        complete = {
            caller for caller, calls in caller_calls.items()
            if calls and calls <= replied.get(caller, set())
        }
        valid_calls = {call_id for call_id, caller in call_to_caller.items() if caller in complete}
        kept: List[EventRecord] = []
        for event in events:
            if event.kind == "assistant_tool_calls":
                if event.event_id in complete:
                    kept.append(event)
            elif event.kind == "tool_result":
                if event.tool_call_id in valid_calls:
                    kept.append(event)
            else:
                kept.append(event)
        return kept

    def _system_prompt(self, session_id: str) -> str:
        prompt = SYSTEM_PROMPT_ZH
        workspace_path = self._workspace_path(session_id)
        if workspace_path:
            prompt += (
                f"\n\n当前工作区（安全边界，可读取）：{workspace_path}\n"
                "你有只读文件工具 fs_list / fs_read / fs_search / fs_project_overview，"
                "只能访问该工作区目录。用户要求阅读、分析项目文件时，直接用这些工具读取。"
            )
        else:
            prompt += (
                "\n\n当前工作区未设置路径，没有可读取的项目目录。"
                "用户要求读文件时，请先说明需要为工作区设置路径。"
            )
        archive = self._store.latest_archive(session_id)
        if archive:
            prompt += "\n\n当前任务状态：\n" + json.dumps(archive["state"], ensure_ascii=False)
            prompt += f"\n最近归档：{archive['archive_id']}，可用 context_search/context_retrieve 展开。"
        return prompt

    def _workspace_path(self, session_id: str) -> str:
        try:
            session = self._store.get_session(session_id)
            workspace_id = session.get("workspace_id")
            if not workspace_id:
                return ""
            return self._store.get_workspace(workspace_id).get("path", "")
        except Exception:  # noqa: BLE001 - workspace lookup is best-effort
            return ""

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
            message: Dict[str, Any] = {
                "role": "assistant",
                "content": event.payload.get("content") or None,
                "tool_calls": calls,
            }
            if event.payload.get("reasoning_content") is not None:
                message["reasoning_content"] = event.payload["reasoning_content"]
            return message
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
