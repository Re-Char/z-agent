import httpx
import pytest

from zagent.domain.errors import ModelTransportError
from zagent.providers.openai_compatible import OpenAICompatibleProvider


def test_provider_posts_native_tools_and_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["model"] == "qwen-test"
        assert body["tools"][0]["function"]["name"] == "context_status"
        assert request.headers["authorization"] == "Bearer key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1", model="qwen-test", api_key="key", client=client
    )
    response = provider.complete(
        [{"role": "user", "content": "你好"}],
        [{"type": "function", "function": {"name": "context_status", "parameters": {}}}],
    )
    assert response.content == "完成"
    provider.close()
    client.close()


def test_provider_maps_http_error_to_transport_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500, text="bad")))
    provider = OpenAICompatibleProvider(base_url="https://model.example/v1", model="glm-test", client=client)
    with pytest.raises(ModelTransportError):
        provider.complete([], [])
    client.close()


def test_provider_surfaces_server_error_detail():
    # DeepSeek-style JSON error body must reach the UI instead of being swallowed.
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Model Not Exist", "type": "invalid_request_error"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(base_url="https://model.example/v1", model="deepseek", client=client)
    with pytest.raises(ModelTransportError) as excinfo:
        provider.complete([], [])
    assert "400" in str(excinfo.value)
    assert "Model Not Exist" in str(excinfo.value)
    client.close()


def test_provider_surfaces_plain_text_error_body():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="Unauthorized")))
    provider = OpenAICompatibleProvider(base_url="https://model.example/v1", model="glm-test", client=client)
    with pytest.raises(ModelTransportError) as excinfo:
        provider.complete([], [])
    assert "Unauthorized" in str(excinfo.value)
    client.close()


def test_provider_requires_base_url():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(base_url="", model="missing")



def test_provider_streams_content_and_assembles_response():
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "，世界"}}]},
            {"choices": [{"delta": {"reasoning_content": "想了一下"}}]},
            {"choices": [{"delta": {}}], "usage": {"total_tokens": 9}},
            "[DONE]",
        ]
        payload = "\n".join(f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks)
        return httpx.Response(200, text=payload + "\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(base_url="https://model.example/v1", model="m", client=client)
    events = list(provider.complete_stream([{"role": "user", "content": "hi"}], []))
    contents = [e["text"] for e in events if e["type"] == "content"]
    reasonings = [e["text"] for e in events if e["type"] == "reasoning"]
    done = next(e for e in events if e["type"] == "done")["response"]
    assert "".join(contents) == "你好，世界"
    assert "".join(reasonings) == "想了一下"
    assert done.content == "你好，世界"
    assert done.reasoning_content == "想了一下"
    assert done.usage["total_tokens"] == 9
    client.close()


def test_provider_stream_accumulates_tool_call_deltas():
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "fs_read", "arguments": ""}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "{\"path\":\"a"}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ".py\"}"}}
            ]}}]},
            "[DONE]",
        ]
        payload = "\n".join(f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks)
        return httpx.Response(200, text=payload + "\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(base_url="https://model.example/v1", model="m", client=client)
    events = list(provider.complete_stream([], []))
    done = next(e for e in events if e["type"] == "done")["response"]
    assert done.tool_calls[0].name == "fs_read"
    assert done.tool_calls[0].arguments == {"path": "a.py"}
    client.close()


def test_provider_stream_surfaces_http_error():
    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = httpx.Client(transport=httpx.MockTransport(unauthorized))
    provider = OpenAICompatibleProvider(base_url="https://model.example/v1", model="m", client=client)
    events = list(provider.complete_stream([], []))
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert errors and "bad key" in errors[0]
    client.close()


def test_provider_retries_transient_status_but_not_bad_request():
    attempts = 0

    def transient(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "恢复"}}]})

    client = httpx.Client(transport=httpx.MockTransport(transient))
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1", model="m", client=client,
        max_retries=2, retry_backoff_seconds=0,
    )
    assert provider.complete([], []).content == "恢复"
    assert attempts == 3
    client.close()

    bad_attempts = 0

    def bad_request(_: httpx.Request) -> httpx.Response:
        nonlocal bad_attempts
        bad_attempts += 1
        return httpx.Response(400, text="invalid")

    client = httpx.Client(transport=httpx.MockTransport(bad_request))
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1", model="m", client=client,
        max_retries=2, retry_backoff_seconds=0,
    )
    with pytest.raises(ModelTransportError):
        provider.complete([], [])
    assert bad_attempts == 1
    client.close()
