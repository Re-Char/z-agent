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


class CombinedToolExecutor:
    """Merges several tool executors into one schema list + dispatch table."""

    def __init__(self, *executors: ToolExecutor) -> None:
        self._executors = list(executors)
        self._by_name: Dict[str, ToolExecutor] = {}

    @property
    def schemas(self) -> list[dict]:
        merged: list[dict] = []
        seen = set()
        for executor in self._executors:
            for schema in executor.schemas:
                name = schema.get("function", {}).get("name")
                if name and name not in seen:
                    merged.append(schema)
                    seen.add(name)
                    self._by_name[name] = executor
        return merged

    def execute(self, session_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        executor = self._by_name.get(name)
        if executor is None:
            self._refresh_dispatch()
            executor = self._by_name.get(name)
            if executor is None:
                raise ToolExecutionError(f"tool is not enabled: {name}")
        return executor.execute(session_id, name, arguments)

    def _refresh_dispatch(self) -> None:
        for executor in self._executors:
            for schema in executor.schemas:
                name = schema.get("function", {}).get("name")
                if name:
                    self._by_name[name] = executor
