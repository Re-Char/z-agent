import pytest

from zagent.domain.errors import ModelProtocolError
from zagent.providers.parser import parse_openai_compatible_response


def test_parse_native_tool_call():
    response = parse_openai_compatible_response({"choices": [{"message": {
        "content": "", "tool_calls": [{"id": "call_1", "function": {
            "name": "context_search", "arguments": '{"query":"中文"}'
        }}]
    }}]})
    assert response.tool_calls[0].name == "context_search"
    assert response.tool_calls[0].arguments == {"query": "中文"}


def test_parse_legacy_function_call():
    response = parse_openai_compatible_response({"choices": [{"message": {
        "function_call": {"name": "context_status", "arguments": "{}"}
    }}]})
    assert response.tool_calls[0].name == "context_status"


def test_invalid_tool_json_is_rejected():
    with pytest.raises(ModelProtocolError):
        parse_openai_compatible_response({"choices": [{"message": {"tool_calls": [{
            "function": {"name": "context_search", "arguments": "not-json"}
        }]}}]})


def test_missing_choices_is_rejected():
    with pytest.raises(ModelProtocolError):
        parse_openai_compatible_response({})
