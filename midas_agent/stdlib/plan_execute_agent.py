"""PlanExecuteAgent — Plan then Execute two-phase agent."""
from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

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
        env_context_xml: str | None = None,
        balance_provider: Callable[[], int] | None = None,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        system_llm: Callable[[LLMRequest], LLMResponse] | None = None,
        action_log: "IO | None" = None,
    ) -> None:
        super().__init__(
            system_prompt, actions, call_llm, max_iterations,
            balance_provider=balance_provider,
            max_tool_output_chars=max_tool_output_chars,
            max_context_tokens=max_context_tokens,
            system_llm=system_llm,
            action_log=action_log,
        )
        self.env_context_xml = env_context_xml

    def run(self, context: str | None = None) -> AgentResult:
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError

        iterations = 0
        action_history: list[ActionRecord] = []
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]

        # Build user message with budget info and task context
        user_parts: list[str] = []

        if self.env_context_xml is not None:
            user_parts.append(self.env_context_xml)

        if context is not None:
            user_parts.append(f"\nTask:\n{context}")

        messages.append({"role": "user", "content": "\n".join(user_parts)})

        # ReAct loop (tools available from the start)
        plan_received = False
        while True:
            if self.max_iterations is not None and iterations >= self.max_iterations:
                logger.info("  Hit max_iterations (%d). Stopping.", self.max_iterations)
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="max_iterations",
                    action_history=action_history,
                )

            try:
                request = LLMRequest(messages=messages, model="default", tools=self._build_tools())
                response = self.call_llm(request)
            except BudgetExhaustedError:
                logger.info("  Budget exhausted at iter %d", iterations + 1)
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="budget_exhausted",
                    action_history=action_history,
                )

            iterations += 1
            resp_tokens = response.usage.input_tokens + response.usage.output_tokens

            if response.tool_calls:
                import json as _json
                assistant_msg: dict = {"role": "assistant"}
                if response.content:
                    assistant_msg["content"] = response.content
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments if isinstance(tc.arguments, str) else _json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages.append(assistant_msg)

                for tool_call in response.tool_calls:
                    action = self._actions_by_name.get(tool_call.name)
                    if action is None:
                        logger.warning("  [iter %d] Unknown tool: %s", iterations, tool_call.name[:80])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"Error: unknown tool '{tool_call.name}'. Available tools: {', '.join(self._actions_by_name.keys())}",
                        })
                        continue
                    logger.info(
                        "  [iter %d] %s(%s) (%d tokens)",
                        iterations,
                        tool_call.name,
                        ", ".join(f"{k}={repr(v)[:80]}" for k, v in tool_call.arguments.items()),
                        resp_tokens,
                    )
                    result = action.execute(**tool_call.arguments)
                    logger.info("    → %s", result[:300] if result else "(empty)")

                    # Write full output to action log BEFORE truncation
                    if self.action_log is not None:
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

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

                    if tool_call.name == "task_done":
                        logger.info("  Task done at iter %d", iterations)
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
                # Planning phase: if tools are available and the agent has
                # not yet received a plan, treat this first content-only
                # response as the plan and continue to execution.
                if self.actions and not plan_received:
                    plan_received = True
                    logger.info(
                        "  [iter %d] Plan response (%d tokens): %s",
                        iterations, resp_tokens, response.content[:300],
                    )
                    messages.append({"role": "assistant", "content": response.content})
                    continue

                logger.info(
                    "  [iter %d] Text response (no tool call, %d tokens): %s",
                    iterations, resp_tokens, response.content[:300],
                )
                return AgentResult(
                    output=response.content,
                    iterations=iterations,
                    termination_reason="done",
                    action_history=action_history,
                )
            else:
                logger.info("  [iter %d] Empty response (no content, no tool calls)", iterations)
                return AgentResult(
                    output="",
                    iterations=iterations,
                    termination_reason="no_action",
                    action_history=action_history,
                )
