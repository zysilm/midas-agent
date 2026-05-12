"""Unit tests for LiteLLMProvider."""
from unittest.mock import patch, MagicMock

import pytest

from llm_agent_toolkit.llm.litellm_provider import LiteLLMProvider
from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse, TokenUsage


def _make_request() -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="test",
    )


def _mock_litellm_response(content="hi", prompt_tokens=10, completion_tokens=5):
    """Build a mock that mimics litellm's response structure."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    message = MagicMock()
    message.content = content
    message.tool_calls = None

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


@pytest.mark.unit
class TestLiteLLMProvider:
    @patch("midas_agent.llm.litellm_provider.litellm")
    def test_returns_llm_response(self, mock_litellm):
        mock_litellm.completion.return_value = _mock_litellm_response()
        provider = LiteLLMProvider(model="gpt-4o")

        result = provider.complete(_make_request())

        assert isinstance(result, LLMResponse)
        assert result.content == "hi"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    @patch("midas_agent.llm.litellm_provider.litellm")
    def test_passes_model_to_litellm(self, mock_litellm):
        mock_litellm.completion.return_value = _mock_litellm_response()
        provider = LiteLLMProvider(model="claude-sonnet-4-20250514")

        provider.complete(_make_request())

        call_kwargs = mock_litellm.completion.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"

    @patch("midas_agent.llm.litellm_provider.litellm")
    def test_passes_api_key(self, mock_litellm):
        mock_litellm.completion.return_value = _mock_litellm_response()
        provider = LiteLLMProvider(model="gpt-4o", api_key="sk-test")

        provider.complete(_make_request())

        call_kwargs = mock_litellm.completion.call_args[1]
        assert call_kwargs["api_key"] == "sk-test"

    @patch("midas_agent.llm.litellm_provider.litellm")
    def test_no_api_key_by_default(self, mock_litellm):
        mock_litellm.completion.return_value = _mock_litellm_response()
        provider = LiteLLMProvider(model="gpt-4o")

        provider.complete(_make_request())

        call_kwargs = mock_litellm.completion.call_args[1]
        assert "api_key" not in call_kwargs

    @patch("midas_agent.llm.litellm_provider.litellm")
    def test_maps_tool_calls(self, mock_litellm):
        tc = MagicMock()
        tc.id = "call-1"
        tc.function.name = "bash"
        tc.function.arguments = '{"command": "ls"}'

        message = MagicMock()
        message.content = None
        message.tool_calls = [tc]

        choice = MagicMock()
        choice.message = message

        usage = MagicMock()
        usage.prompt_tokens = 20
        usage.completion_tokens = 10

        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = usage

        mock_litellm.completion.return_value = resp
        provider = LiteLLMProvider(model="gpt-4o")

        result = provider.complete(_make_request())

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "bash"
        assert result.tool_calls[0].arguments == {"command": "ls"}

    @patch("midas_agent.llm.litellm_provider.litellm")
    def test_passes_tools_and_max_tokens(self, mock_litellm):
        mock_litellm.completion.return_value = _mock_litellm_response()
        provider = LiteLLMProvider(model="gpt-4o")

        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            tools=[{"type": "function", "function": {"name": "bash"}}],
            max_tokens=500,
        )
        provider.complete(request)

        call_kwargs = mock_litellm.completion.call_args[1]
        assert call_kwargs["max_tokens"] == 500
        assert call_kwargs["tools"] == request.tools
