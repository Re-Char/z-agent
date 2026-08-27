from __future__ import annotations

import time
from dataclasses import dataclass

from zagent.context.orchestrator import ContextOrchestrator
from zagent.domain.errors import AgentLimitError
from zagent.domain.models import AgentResult
from zagent.providers.base import ModelProvider
from zagent.storage.sqlite_store import SqliteStore

from .tools import ToolExecutor


@dataclass(frozen=True)
class AgentRuntimeLimits:
    max_tool_rounds: int = 8
    task_timeout_seconds: float = 300


class AgentRuntime:
    """Self-contained model/tool loop; no agent framework or hosted execution."""

    def __init__(
        self,
        store: SqliteStore,
        context: ContextOrchestrator,
        provider: ModelProvider,
        tools: ToolExecutor,
        limits: AgentRuntimeLimits | None = None,
    ) -> None:
        self._store = store
        self._context = context
        self._provider = provider
        self._tools = tools
        self._limits = limits or AgentRuntimeLimits()

    def send(self, session_id: str, content: str) -> AgentResult:
        user_event = self._store.append_event(
            session_id, "message", "user", content, provenance="user"
        )
        deadline = time.monotonic() + self._limits.task_timeout_seconds
        tool_rounds = 0
        latest_usage = None

        while True:
            if time.monotonic() >= deadline:
                raise AgentLimitError("agent task deadline exceeded")
            working_set = self._context.build_working_set(session_id)
            response = self._provider.complete(working_set.messages, self._tools.schemas)
            latest_usage = response.usage
            if response.raw is not None:
                self._store.append_event(
                    session_id, "model_raw", "system", response.raw,
                    parent_event_id=user_event.event_id, sensitivity="internal", provenance="model-provider",
                )
            if not response.tool_calls:
                final_event = self._store.append_event(
                    session_id, "message", "assistant", response.content,
                    parent_event_id=user_event.event_id, provenance="model",
                )
                return AgentResult(
                    final_event=final_event,
                    working_set=self._context.build_working_set(session_id),
                    model_usage=latest_usage,
                    tool_rounds=tool_rounds,
                )

            tool_rounds += 1
            if tool_rounds > self._limits.max_tool_rounds:
                raise AgentLimitError("maximum tool rounds exceeded")
            call_payload = {
                "content": response.content,
                "tool_calls": [
                    {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
            }
            self._store.append_event(
                session_id, "assistant_tool_calls", "assistant", call_payload,
                parent_event_id=user_event.event_id, provenance="model",
            )
            for call in response.tool_calls:
                try:
                    result = self._tools.execute(session_id, call.name, call.arguments)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "tool": call.name}
                self._store.append_event(
                    session_id, "tool_result", "tool", result,
                    tool_name=call.name, tool_call_id=call.call_id,
                    provenance="local-tool-runtime",
                )
