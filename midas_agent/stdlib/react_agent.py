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
    @staticmethod
    def _check_stuck(action_history: list[ActionRecord]) -> str | None:
        """Return a warning string if the agent appears stuck, else None."""
        if len(action_history) < 3:
            return None

        # Rule 1: Same file edited 3+ times (anywhere in history)
        from collections import Counter
        edit_counts: Counter[str] = Counter()
        for rec in action_history:
            is_edit = False
            if rec.action_name == "str_replace_editor":
                cmd = rec.arguments.get("command", "")
                is_edit = cmd in ("str_replace", "insert", "create")
            elif rec.action_name == "edit_file":
                is_edit = True
            if is_edit:
                path = rec.arguments.get("path", "")
                if path:
                    edit_counts[path] += 1
        for path, count in edit_counts.items():
            if count >= 3:
                return (
                    f"\u26a0 You have edited {path} {count} times without resolving the issue. "
                    "Your current approach may be wrong. Use the think tool to re-read the "
                    "issue description, reconsider the root cause, and try a completely "
                    "different approach."
                )

        # Rule 2: Last 3 actions have identical action_name AND arguments
        last3 = action_history[-3:]
        if (
            last3[0].action_name == last3[1].action_name == last3[2].action_name
            and last3[0].arguments == last3[1].arguments == last3[2].arguments
        ):
            return (
                f"\u26a0 You have repeated the same action ({last3[0].action_name}) with identical "
                "arguments 3 times in a row. You appear to be stuck. Use the think tool "
                "to reconsider your approach and try something different."
            )

        return None

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
        on_action: Callable | None = None,  # Callable[[ActionEvent], None]
        action_log: "IO | None" = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.actions = actions
        self.call_llm = call_llm
        self.max_iterations = max_iterations
        self.balance_provider = balance_provider
        self.max_tool_output_chars = max_tool_output_chars
        self.max_context_tokens = max_context_tokens
        self.system_llm = system_llm
        self.on_action = on_action
        self.action_log = action_log
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
                if "description" in param_def:
                    prop["description"] = param_def["description"]
                if "default" in param_def:
                    prop["default"] = param_def["default"]
                if "enum" in param_def:
                    prop["enum"] = param_def["enum"]
                if "items" in param_def:
                    prop["items"] = param_def["items"]
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
        total_tokens = 0
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
                logger.info("  Budget exhausted at iter %d (%d tokens)", iterations + 1, total_tokens)
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="budget_exhausted",
                    action_history=action_history,
                )

            iterations += 1
            if response.usage:
                total_tokens += response.usage.input_tokens + response.usage.output_tokens

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
                            "  [iter %d] %s(%s) (%d tokens)",
                            iterations,
                            tool_call.name,
                            ", ".join(f"{k}={repr(v)[:80]}" for k, v in tool_call.arguments.items()),
                            total_tokens,
                        )
                        result = action.execute(**tool_call.arguments)
                        logger.info("    → %s", result[:200] if result else "(empty)")

                    # Write full output to action log BEFORE truncation
                    if self.action_log is not None:
                        import json as _json
                        self.action_log.write(_json.dumps({
                            "iter": iterations,
                            "action": tool_call.name,
                            "args": tool_call.arguments,
                            "result": result,
                            "timestamp": time.time(),
                        }) + "\n")
                        self.action_log.flush()

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

                    if action is not None and self.on_action is not None:
                        from midas_agent.tui import ActionEvent
                        self.on_action(ActionEvent(
                            action_name=tool_call.name,
                            arguments=tool_call.arguments,
                            result=result,
                        ))

                    # Add tool result message
                    tool_content = result
                    if self.balance_provider is not None:
                        tool_content += f"\n[当前余额: {self.balance_provider()}]"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_content,
                    })

                    # Check for task_done action
                    if tool_call.name == "task_done":
                        logger.info("  Task done at iter %d (%d tokens).", iterations, total_tokens)
                        return AgentResult(
                            output=result,
                            iterations=iterations,
                            termination_reason="done",
                            action_history=action_history,
                        )

                # Check if agent is stuck
                stuck_msg = ReactAgent._check_stuck(action_history)
                if stuck_msg:
                    messages.append({"role": "user", "content": stuck_msg})

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
