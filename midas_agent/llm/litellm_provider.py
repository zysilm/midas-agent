"""LiteLLM-based provider — unified interface to 100+ LLM providers."""
from __future__ import annotations

import json
import re

import litellm

from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall


def _parse_qwen3_coder_tool_calls(content: str) -> list[ToolCall] | None:
    """Parse Qwen3-Coder XML-style tool calls from content text.

    Qwen3-Coder via OpenAI-compatible endpoints returns tool calls as
    XML tags in the content field instead of the tool_calls field:

        <function=bash>
        <parameter=command>ls -la</parameter>
        </function>
    """
    pattern = r"<function=(\w+)>(.*?)</function>"
    matches = re.findall(pattern, content, re.DOTALL)
    if not matches:
        return None

    tool_calls = []
    for i, (func_name, body) in enumerate(matches):
        params = {}
        for param_match in re.finditer(
            r"<parameter=(\w+)>(.*?)</parameter>", body, re.DOTALL
        ):
            params[param_match.group(1)] = param_match.group(2).strip()
        tool_calls.append(ToolCall(
            id=f"qwen3_call_{i}",
            name=func_name,
            arguments=params,
        ))
    return tool_calls or None


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
            kwargs["tool_choice"] = "required"

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
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        # LLM produced invalid JSON escaping — try to salvage
                        import re
                        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', args)
                        try:
                            args = json.loads(fixed)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        # Fallback: parse Hermes-style tool calls from content
        content = message.content
        if not tool_calls and content:
            tool_calls = _parse_qwen3_coder_tool_calls(content)
            if tool_calls:
                content = None  # tool call, not text

        usage = response.usage
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            ),
        )
