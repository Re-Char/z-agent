from __future__ import annotations

from typing import Any, Dict, Protocol

from zagent.context.orchestrator import ContextOrchestrator
from zagent.domain.errors import ToolExecutionError


class ToolExecutor(Protocol):
    @property
    def schemas(self) -> list[dict]: ...

    def execute(self, session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]: ...


class ContextToolExecutor:
    def __init__(self, context: ContextOrchestrator) -> None:
        self._context = context

    @property
    def schemas(self) -> list[dict]:
        return self._context.tool_schemas

    def execute(self, session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not name.startswith("context_"):
            raise ToolExecutionError(f"tool is not enabled in v1: {name}")
        return self._context.execute(session_id, name, arguments)

