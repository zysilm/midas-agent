"""LiteLLM-based provider — unified interface to 100+ LLM providers."""
from __future__ import annotations

import litellm

from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall


class LiteLLMProvider(LLMProvider):
    """Bridges our LLMProvider ABC to litellm.completion()."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "messages": request.messages,
            "temperature": request.temperature,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.tools is not None:
            kwargs["tools"] = request.tools

        response = litellm.completion(**kwargs)

        choice = response.choices[0]
        message = choice.message

        # Map tool calls
        tool_calls: list[ToolCall] | None = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    import json
                    args = json.loads(args)
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        usage = response.usage
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            ),
        )
