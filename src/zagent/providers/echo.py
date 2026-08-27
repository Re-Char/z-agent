from __future__ import annotations

from typing import Any, Dict, List

from zagent.domain.models import ModelResponse


class EchoProvider:
    """Offline provider for bootstrap, demos and functional tests."""

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelResponse:
        latest = next(
            (message.get("content", "") for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        return ModelResponse(content=f"本地演示模式已记录消息：{latest}", raw={"provider": "echo"})
