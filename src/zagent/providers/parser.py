from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

from zagent.domain.errors import ModelProtocolError
from zagent.domain.models import ModelResponse, ToolCall


def parse_openai_compatible_response(raw: Dict[str, Any]) -> ModelResponse:
    choices = raw.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ModelProtocolError("model response does not contain choices[0]")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise ModelProtocolError("model response message is not an object")
    tool_calls = _parse_tool_calls(message)
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    reasoning_content = message.get("reasoning_content")
    if reasoning_content is not None and not isinstance(reasoning_content, str):
        reasoning_content = json.dumps(reasoning_content, ensure_ascii=False)
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        usage=raw.get("usage"),
        raw=raw,
    )


def _parse_tool_calls(message: Dict[str, Any]) -> List[ToolCall]:
    raw_calls = list(message.get("tool_calls") or [])
    legacy = message.get("function_call")
    if legacy:
        raw_calls.append({"id": None, "function": legacy})
    result: List[ToolCall] = []
    for raw_call in raw_calls:
        function = raw_call.get("function") or raw_call
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ModelProtocolError("tool call does not contain a function name")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError(f"tool arguments are not valid JSON: {arguments[:300]}") from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("tool arguments must be a JSON object")
        result.append(ToolCall(call_id=raw_call.get("id") or "call_" + uuid.uuid4().hex,
                               name=name, arguments=arguments))
    return result
