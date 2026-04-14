"""Unit tests for LLM request/response data types."""
import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall


@pytest.mark.unit
class TestLLMRequest:
    """Tests for the LLMRequest data class."""

    def test_llm_request_required_fields(self):
        """Creating an LLMRequest with messages and model sets those fields correctly."""
        messages = [{"role": "user", "content": "hello"}]
        req = LLMRequest(messages=messages, model="gpt-4")

        assert req.messages == messages
        assert req.model == "gpt-4"

    def test_llm_request_defaults(self):
        """LLMRequest defaults: temperature=0.0, reasoning_effort=None, tools=None, max_tokens=None."""
        req = LLMRequest(messages=[], model="gpt-4")

        assert req.temperature == 0.0
        assert req.reasoning_effort is None
        assert req.tools is None
        assert req.max_tokens is None

    def test_llm_request_all_fields(self):
        """Creating an LLMRequest with all fields explicitly set."""
        tools = [{"type": "function", "name": "search"}]
        req = LLMRequest(
            messages=[{"role": "system", "content": "You are helpful."}],
            model="claude-3",
            temperature=0.7,
            reasoning_effort="high",
            tools=tools,
            max_tokens=1024,
        )

        assert req.messages == [{"role": "system", "content": "You are helpful."}]
        assert req.model == "claude-3"
        assert req.temperature == 0.7
        assert req.reasoning_effort == "high"
        assert req.tools == tools
        assert req.max_tokens == 1024


@pytest.mark.unit
class TestLLMResponse:
    """Tests for the LLMResponse data class."""

    def test_llm_response_with_content(self):
        """LLMResponse with text content and no tool calls."""
        usage = TokenUsage(input_tokens=10, output_tokens=20)
        resp = LLMResponse(content="Hello!", tool_calls=None, usage=usage)

        assert resp.content == "Hello!"
        assert resp.tool_calls is None
        assert resp.usage is usage

    def test_llm_response_with_tool_calls(self):
        """LLMResponse with tool calls and no text content."""
        tool_call = ToolCall(id="tc_1", name="search", arguments={"q": "test"})
        usage = TokenUsage(input_tokens=5, output_tokens=15)
        resp = LLMResponse(content=None, tool_calls=[tool_call], usage=usage)

        assert resp.content is None
        assert resp.tool_calls == [tool_call]
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "search"

    def test_llm_response_usage_required(self):
        """LLMResponse requires the usage field; omitting it raises TypeError."""
        with pytest.raises(TypeError):
            LLMResponse(content="hi", tool_calls=None)  # type: ignore[call-arg]


@pytest.mark.unit
class TestTokenUsage:
    """Tests for the TokenUsage data class."""

    def test_token_usage_total(self):
        """input_tokens + output_tokens gives the total token count."""
        usage = TokenUsage(input_tokens=100, output_tokens=50)

        assert usage.input_tokens + usage.output_tokens == 150


@pytest.mark.unit
class TestToolCall:
    """Tests for the ToolCall data class."""

    def test_tool_call_fields(self):
        """ToolCall stores id, name, and arguments correctly."""
        tc = ToolCall(id="call_abc", name="bash", arguments={"cmd": "ls"})

        assert tc.id == "call_abc"
        assert tc.name == "bash"
        assert tc.arguments == {"cmd": "ls"}
