"""PlanExecuteAgent — Plan then Execute two-phase agent."""
from __future__ import annotations

import time
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import Action
from midas_agent.stdlib.react_agent import ActionRecord, AgentResult, ReactAgent


class PlanExecuteAgent(ReactAgent):
    def __init__(
        self,
        system_prompt: str,
        actions: list[Action],
        call_llm: Callable[[LLMRequest], LLMResponse],
        max_iterations: int | None = None,
        market_info_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(system_prompt, actions, call_llm, max_iterations)
        self.market_info_provider = market_info_provider

    def run(self, context: str | None = None) -> AgentResult:
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError

        iterations = 0
        action_history: list[ActionRecord] = []
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]

        # Build planning prompt with market info and context
        planning_parts: list[str] = []
        planning_parts.append("Create a plan for the following task.")

        if self.market_info_provider is not None:
            market_info = self.market_info_provider()
            planning_parts.append(f"Market info: {market_info}")

        if context is not None:
            planning_parts.append(f"Task context: {context}")

        messages.append({"role": "user", "content": "\n".join(planning_parts)})

        # Planning phase: call LLM once to get a plan
        try:
            plan_request = LLMRequest(messages=messages, model="default")
            plan_response = self.call_llm(plan_request)
        except BudgetExhaustedError:
            return AgentResult(
                output="",
                iterations=iterations,
                termination_reason="budget_exhausted",
                action_history=action_history,
            )

        iterations += 1
        plan_text = plan_response.content or ""

        # Add plan to conversation as assistant response
        messages.append({"role": "assistant", "content": plan_text})

        # Add execution instruction
        messages.append({
            "role": "user",
            "content": "Now execute the plan step by step.",
        })

        # Execution phase: standard ReAct loop
        while True:
            if self.max_iterations is not None and iterations >= self.max_iterations:
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="max_iterations",
                    action_history=action_history,
                )

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

            if response.tool_calls:
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

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

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
