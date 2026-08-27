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


def test_provider_requires_base_url():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(base_url="", model="missing")

