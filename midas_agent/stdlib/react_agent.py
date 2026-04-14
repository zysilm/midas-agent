"""ReactAgent — ReAct loop implementation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import Action


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
    ) -> None:
        self.system_prompt = system_prompt
        self.actions = actions
        self.call_llm = call_llm
        self.max_iterations = max_iterations
        self._actions_by_name: dict[str, Action] = {a.name: a for a in actions}

    def run(self, context: str | None = None) -> AgentResult:
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError

        iterations = 0
        action_history: list[ActionRecord] = []
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]

        if context is not None:
            messages.append({"role": "user", "content": context})

        while True:
            try:
                request = LLMRequest(messages=messages, model="default")
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
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": response.tool_calls,
                })

                for tool_call in response.tool_calls:
                    action = self._actions_by_name[tool_call.name]
                    result = action.execute(**tool_call.arguments)

                    record = ActionRecord(
                        action_name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                        timestamp=time.time(),
                    )
                    action_history.append(record)

                    # Add tool result message
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

                    # Check for task_done action
                    if tool_call.name == "task_done":
                        return AgentResult(
                            output=result,
                            iterations=iterations,
                            termination_reason="done",
                            action_history=action_history,
                        )
            elif response.content:
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
