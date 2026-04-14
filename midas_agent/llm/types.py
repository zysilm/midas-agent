"""LLM request/response types."""
from dataclasses import dataclass


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMRequest:
    messages: list[dict]
    model: str
    temperature: float = 0.0
    reasoning_effort: str | None = None
    tools: list[dict] | None = None
    max_tokens: int | None = None


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] | None
    usage: TokenUsage
