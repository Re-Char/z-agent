from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, List, Optional

import httpx

from zagent.domain.errors import ModelTransportError
from zagent.domain.models import ModelResponse, ToolCall

from .parser import parse_openai_compatible_response


def _usage_int(usage: Optional[Dict[str, Any]], *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.2,
        timeout_seconds: float = 120,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._endpoint = self._chat_endpoint(base_url)
        self._model = model
        self._temperature = temperature
        self._max_retries = max(0, max_retries)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    @staticmethod
    def _chat_endpoint(base_url: str) -> str:
        endpoint = base_url.rstrip("/")
        return endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelResponse:
        payload = self._payload(messages, tools, stream=False)
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(self._endpoint, headers=self._headers, json=payload)
                response.raise_for_status()
                raw = response.json()
                break
            except httpx.HTTPStatusError as exc:
                if self._should_retry(exc.response.status_code, attempt):
                    self._backoff(attempt)
                    continue
                detail = self._error_detail(exc.response)
                raise ModelTransportError(
                    f"model request failed ({exc.response.status_code}): {detail}"
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    self._backoff(attempt)
                    continue
                raise ModelTransportError(f"model request failed: {exc}") from exc
            except ValueError as exc:
                raise ModelTransportError(f"model request failed: {exc}") from exc
        return parse_openai_compatible_response(raw)

    def complete_stream(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Iterator[Dict[str, Any]]:
        """Stream a completion, yielding structured events:
        {"type": "content"|"reasoning", "text": str}
        {"type": "tool_call", "index": int, "call_id": str, "name": str, "arguments_delta": str}
        {"type": "done", "response": ModelResponse}
        {"type": "error", "message": str}
        """
        payload = self._payload(messages, tools, stream=True)
        for attempt in range(self._max_retries + 1):
            try:
                with self._client.stream(
                    "POST", self._endpoint, headers=self._headers, json=payload
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        if self._should_retry(response.status_code, attempt):
                            self._backoff(attempt)
                            continue
                        detail = self._error_detail(response)
                        yield {
                            "type": "error",
                            "message": f"model request failed ({response.status_code}): {detail}",
                        }
                        return
                    yield from self._iter_sse(response)
                    return
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    self._backoff(attempt)
                    continue
                yield {"type": "error", "message": f"model request failed: {exc}"}
                return

    def _should_retry(self, status_code: int, attempt: int) -> bool:
        return attempt < self._max_retries and (status_code == 429 or status_code >= 500)

    def _backoff(self, attempt: int) -> None:
        time.sleep(self._retry_backoff_seconds * (2 ** attempt))

    def _payload(self, messages, tools, *, stream: bool) -> Dict[str, Any]:
        return {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": self._temperature,
            "stream": stream,
        }

    def _iter_sse(self, response: httpx.Response) -> Iterator[Dict[str, Any]]:
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        calls: Dict[int, Dict[str, Any]] = {}
        usage = None
        try:
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                    yield {"type": "reasoning", "text": delta["reasoning_content"]}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    yield {"type": "content", "text": delta["content"]}
                for raw_call in delta.get("tool_calls") or []:
                    index = int(raw_call.get("index", 0))
                    slot = calls.setdefault(index, {"call_id": "", "name": "", "arguments": ""})
                    function = raw_call.get("function") or {}
                    if raw_call.get("id"):
                        slot["call_id"] = raw_call["id"]
                    if function.get("name"):
                        slot["name"] = function["name"]
                        yield self._tool_call_event(index, slot)
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]
                        yield self._tool_call_event(index, slot)
        except (httpx.HTTPError, ValueError) as exc:
            yield {"type": "error", "message": f"model stream failed: {exc}"}
            return

        tool_calls: List[ToolCall] = []
        for index in sorted(calls):
            slot = calls[index]
            if not slot["name"]:
                continue
            arguments = slot["arguments"] or "{}"
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_arguments = {"_raw": arguments}
            tool_calls.append(ToolCall(
                call_id=slot["call_id"] or f"call_{index}",
                name=slot["name"],
                arguments=parsed_arguments,
            ))

        response = ModelResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts) or None,
            usage=usage,
            raw={"streamed": True, "usage": usage},
        )
        yield {"type": "done", "response": response}

    @staticmethod
    def _tool_call_event(index: int, slot: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "tool_call",
            "index": index,
            "call_id": slot["call_id"],
            "name": slot["name"],
            "arguments_delta": slot["arguments"],
        }

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = error.get("message") or error.get("code") or error
            else:
                detail = error or payload
            rendered = str(detail)
        except ValueError:
            rendered = response.text.strip()
        return (rendered or response.reason_phrase or "unknown model API error")[:1200]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
