from __future__ import annotations

import json
from typing import Any, Dict

from pydantic import ValidationError as PydanticValidationError

from zagent.domain.errors import ToolExecutionError
from zagent.storage.sqlite_store import SqliteStore

from .tool_arguments import CONTEXT_ARGUMENT_TYPES, StrictArgs, context_tool_schemas
from .working_set import WorkingSetBuilder


class ContextOrchestrator:
    def __init__(self, store: SqliteStore, working_sets: WorkingSetBuilder) -> None:
        self._store = store
        self._working_sets = working_sets

    @property
    def tool_schemas(self) -> list[dict]:
        return context_tool_schemas()

    def build_working_set(self, session_id: str):
        return self._working_sets.build(session_id)

    def execute(self, session_id: str, tool_name: str, raw_arguments: Dict[str, Any]) -> Dict[str, Any]:
        argument_type = CONTEXT_ARGUMENT_TYPES.get(tool_name)
        if argument_type is None:
            raise ToolExecutionError(f"unknown context tool: {tool_name}")
        try:
            arguments = argument_type.model_validate(raw_arguments)
        except PydanticValidationError as exc:
            raise ToolExecutionError(f"invalid arguments for {tool_name}: {exc}") from exc
        return self._dispatch(session_id, tool_name, arguments)

    def _dispatch(self, session_id: str, tool_name: str, arguments: StrictArgs) -> Dict[str, Any]:
        values = arguments.model_dump()
        if tool_name == "context_status":
            working_set = self._working_sets.build(session_id)
            return {
                "stats": self._store.session_stats(session_id),
                "working_set": {
                    "tokens": working_set.token_estimate,
                    "budget": working_set.budget,
                    "included_event_ids": working_set.included_event_ids,
                    "pinned_event_ids": working_set.pinned_event_ids,
                },
                "latest_archive": self._store.latest_archive(session_id),
            }
        if tool_name == "context_search":
            return {"results": self._store.search_events(session_id, values["query"], values["limit"])}
        if tool_name == "context_retrieve":
            return {"events": self._retrieve(session_id, values["event_ids"], values["max_chars"])}
        if tool_name == "context_archive":
            return self._store.create_archive(
                session_id,
                values["start_sequence"],
                values["end_sequence"],
                values["reason"],
                values["state_update"],
            )
        if tool_name == "context_pin":
            for event_id in values["event_ids"]:
                self._store.pin_event(session_id, event_id, values["rationale"])
            return {"pinned": values["event_ids"]}
        if tool_name == "context_unpin":
            for event_id in values["event_ids"]:
                self._store.unpin_event(session_id, event_id)
            return {"unpinned": values["event_ids"]}
        raise ToolExecutionError(f"unhandled context tool: {tool_name}")

    def _retrieve(self, session_id: str, event_ids: list[str], max_chars: int) -> list[dict]:
        items: list[dict] = []
        used_chars = 0
        for event_id in event_ids:
            event = self._store.get_event(event_id)
            if event.session_id != session_id:
                continue
            serialized = (
                event.payload
                if isinstance(event.payload, str)
                else json.dumps(event.payload, ensure_ascii=False)
            )
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            item = event.to_dict()
            item["payload"] = serialized[:remaining]
            item["truncated"] = len(serialized) > remaining
            used_chars += min(len(serialized), remaining)
            items.append(item)
        return items
