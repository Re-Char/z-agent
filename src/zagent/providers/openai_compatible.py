from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from zagent.domain.errors import ModelTransportError
from zagent.domain.models import ModelResponse

from .parser import parse_openai_compatible_response


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.2,
        timeout_seconds: float = 120,
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._endpoint = self._chat_endpoint(base_url)
        self._model = model
        self._temperature = temperature
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    @staticmethod
    def _chat_endpoint(base_url: str) -> str:
        endpoint = base_url.rstrip("/")
        return endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelResponse:
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": self._temperature,
            "stream": False,
        }
        try:
            response = self._client.post(self._endpoint, headers=self._headers, json=payload)
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelTransportError(f"model request failed: {exc}") from exc
        return parse_openai_compatible_response(raw)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

