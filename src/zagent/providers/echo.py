from __future__ import annotations

from typing import Any, Dict, Iterator, List

from zagent.domain.models import ModelResponse


class EchoProvider:
    """Offline provider for bootstrap, demos and functional tests."""

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelResponse:
        latest = next(
            (message.get("content", "") for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        return ModelResponse(content=f"本地演示模式已记录消息：{latest}", raw={"provider": "echo"})

    def complete_stream(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Iterator[Dict[str, Any]]:
        response = self.complete(messages, tools)
        # Chunk the reply so the UI streaming path can be exercised offline too.
        for piece in _chunk_text(response.content, size=8):
            yield {"type": "content", "text": piece}
        yield {"type": "done", "response": response}


def _chunk_text(text: str, size: int) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]
