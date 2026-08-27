from __future__ import annotations

from typing import Any, Dict, List, Protocol

from zagent.domain.models import ModelResponse


class ModelProvider(Protocol):
    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelResponse:
        """Return a normalized response. Providers never execute tools."""
        ...

