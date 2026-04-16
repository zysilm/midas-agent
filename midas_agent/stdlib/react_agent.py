"""ReactAgent — ReAct loop implementation."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import Action

logger = logging.getLogger(__name__)


@dataclass
class ActionRecord:
    action_name: str
    arguments: dict
    result: str
    timestamp: float


@dataclass
class AgentResult:
    output: str
    iterations: int
    termination_reason: str  # "done" | "budget_exhausted" | "max_iterations" | "no_action"
    action_history: list[ActionRecord] = field(default_factory=list)


class ReactAgent:
    def __init__(
        self,
        system_prompt: str,
        actions: list[Action],
        call_llm: Callable[[LLMRequest], LLMResponse],
        max_iterations: int | None = None,
        balance_provider: Callable[[], int] | None = None,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        system_llm: Callable[[LLMRequest], LLMResponse] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.actions = actions
        self.call_llm = call_llm
        self.max_iterations = max_iterations
        self.balance_provider = balance_provider
        self.max_tool_output_chars = max_tool_output_chars
        self.max_context_tokens = max_context_tokens
        self.system_llm = system_llm
        self._actions_by_name: dict[str, Action] = {a.name: a for a in actions}

    def _build_tools(self) -> list[dict] | None:
        """Convert Action objects to OpenAI tools format."""
        if not self.actions:
            return None
        tools = []
        for action in self.actions:
            properties = {}
            required = []
            for param_name, param_def in action.parameters.items():
                prop = {"type": param_def.get("type", "string")}
                if "default" in param_def:
                    prop["default"] = param_def["default"]
                properties[param_name] = prop
                if param_def.get("required", False):
                    required.append(param_name)
            tools.append({
                "type": "function",
                "function": {
                    "name": action.name,
                    "description": action.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return tools

    def run(self, context: str | None = None) -> AgentResult:
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError

        iterations = 0
        action_history: list[ActionRecord] = []
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        tools = self._build_tools()

        if context is not None:
            messages.append({"role": "user", "content": context})

        while True:
            try:
                request = LLMRequest(messages=messages, model="default", tools=tools)
                response = self.call_llm(request)
            except BudgetExhaustedError:
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="budget_exhausted",
                    action_history=action_history,
                )

            iterations += 1

            # Check iteration limit after incrementing
            if self.max_iterations is not None and iterations >= self.max_iterations:
                return AgentResult(
                    output=response.content or "",
                    iterations=iterations,
                    termination_reason="max_iterations",
                    action_history=action_history,
                )

            if response.tool_calls:
                # Build assistant message with tool calls
                assistant_msg: dict = {"role": "assistant"}
                if response.content:
                    assistant_msg["content"] = response.content
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments if isinstance(tc.arguments, str) else __import__("json").dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages.append(assistant_msg)

                for tool_call in response.tool_calls:
                    action = self._actions_by_name.get(tool_call.name)
                    if action is None:
                        available = ", ".join(sorted(self._actions_by_name.keys()))
                        result = (
                            f"Error: tool '{tool_call.name}' is not available. "
                            f"Available tools: {available}"
                        )
                        logger.warning(
                            "  [iter %d] Unknown tool: %s",
                            iterations,
                            tool_call.name,
                        )
                    else:
                        logger.info(
                            "  [iter %d] %s(%s)",
                            iterations,
                            tool_call.name,
                            ", ".join(f"{k}={repr(v)[:80]}" for k, v in tool_call.arguments.items()),
                        )
                        result = action.execute(**tool_call.arguments)
                        logger.info("    → %s", result[:200] if result else "(empty)")

                    # Truncate large tool output before it enters conversation history
                    if self.max_tool_output_chars is not None and result and len(result) > self.max_tool_output_chars:
                        from midas_agent.context.truncation import truncate_output
                        result = truncate_output(result, max_chars=self.max_tool_output_chars)

                    record = ActionRecord(
                        action_name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                        timestamp=time.time(),
                    )
                    action_history.append(record)

                    # Add tool result message
                    tool_content = result
                    if self.balance_provider is not None:
                        tool_content += f"\n[当前余额: {self.balance_provider()}]"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_content,
                    })

                    # Check for task_done or report_result action
                    if tool_call.name in ("task_done", "report_result"):
                        logger.info("  Task done.")
                        return AgentResult(
                            output=result,
                            iterations=iterations,
                            termination_reason="done",
                            action_history=action_history,
                        )

                # Compaction check after processing all tool calls
                if self.max_context_tokens and self.system_llm:
                    total_chars = sum(len(m.get("content", "")) for m in messages)
                    total_tokens_est = total_chars // 4
                    from midas_agent.context.compaction import should_compact, build_compaction_prompt, build_compacted_history
                    if should_compact(total_tokens_est, self.max_context_tokens):
                        compact_prompt = build_compaction_prompt(messages)
                        compact_request = LLMRequest(messages=compact_prompt, model="default")
                        compact_response = self.system_llm(compact_request)
                        summary = compact_response.content or ""
                        messages = build_compacted_history(messages, summary)
                        if not messages or messages[0].get("role") != "system":
                            messages.insert(0, {"role": "system", "content": self.system_prompt})
            elif response.content:
                logger.info("  [iter %d] Response: %s", iterations, response.content[:200])
                return AgentResult(
                    output=response.content,
                    iterations=iterations,
                    termination_reason="done",
                    action_history=action_history,
                )
            else:
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="no_action",
                    action_history=action_history,
                )
