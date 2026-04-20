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
        env_cwd: str | None = None,
        env_agents: list[str] | None = None,
    ) -> None:
        super().__init__(
            system_prompt, actions, call_llm, max_iterations,
            balance_provider=balance_provider,
            max_tool_output_chars=max_tool_output_chars,
            max_context_tokens=max_context_tokens,
            system_llm=system_llm,
            action_log=action_log,
        )
        self.env_context_xml = env_context_xml  # kept for backward compat
        self._env_cwd = env_cwd
        self._env_agents = env_agents or []

    @staticmethod
    def _build_parent_context_summary(messages: list[dict], max_result_chars: int = 200) -> str:
        """Build a truncated summary of the parent agent's conversation.

        Keeps system/user messages in full. Truncates tool results to
        max_result_chars so the sub-agent knows what was done without
        the full raw data.
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                continue  # sub-agent gets its own system prompt

            if role == "user":
                lines.append(f"[user] {content}")

            elif role == "assistant":
                # Summarize tool calls
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        name = func.get("name", "?")
                        args = func.get("arguments", "")
                        if isinstance(args, str) and len(args) > 200:
                            args = args[:200] + "..."
                        lines.append(f"[action] {name}({args})")
                elif content:
                    lines.append(f"[assistant] {content[:200]}")

            elif role == "tool":
                result = content or ""
                if len(result) > max_result_chars:
                    result = result[:max_result_chars] + "... (truncated)"
                lines.append(f"[result] {result}")

        return "\n".join(lines)

    def _build_env_context_xml(self, iteration: int) -> str:
        """Build environment context XML with live data."""
        from midas_agent.context.environment import EnvironmentContext

        balance = self.balance_provider() if self.balance_provider else None
        env = EnvironmentContext(
            cwd=self._env_cwd,
            shell="bash",
            balance=balance,
            iteration=iteration,
            available_agents=self._env_agents,
        )
        return env.serialize_to_xml()

    def _planning_step(self, messages: list[dict], iteration: int) -> bool:
        """Ask the LLM whether to delegate. Returns True if delegate."""
        import json as _json
        from midas_agent.prompts import PLANNING_PROMPT

        env_xml = self._build_env_context_xml(iteration)
        planning_prompt = PLANNING_PROMPT.format(env_context=env_xml)

        planning_messages = [
            {"role": "system", "content": self.system_prompt},
        ]
        # Include conversation history so the LLM knows current state
        planning_messages.extend(messages[1:])
        planning_messages.append({"role": "user", "content": planning_prompt})

        try:
            request = LLMRequest(
                messages=planning_messages,
                model="default",
                max_tokens=200,
            )
            response = self.call_llm(request)
            content = response.content or ""

            # Parse JSON from response (handle reasoning models that wrap in text)
            try:
                # Try direct parse
                decision = _json.loads(content.strip())
            except _json.JSONDecodeError:
                # Try to extract JSON from text
                import re
                match = re.search(r'\{[^}]+\}', content)
                if match:
                    decision = _json.loads(match.group())
                else:
                    decision = {"delegate": False}

            delegate = decision.get("delegate", False)
            if delegate:
                task = decision.get("task", "")
                logger.info("  [iter %d] Planning: delegate — %s", iteration + 1, task)
            else:
                logger.info("  [iter %d] Planning: act directly", iteration + 1)
            return delegate, decision.get("task", "")
        except Exception as e:
            logger.info("  [iter %d] Planning failed (%s), acting directly", iteration + 1, e)
            return False, ""

    def _build_tools_filtered(self, delegate: bool) -> list[dict] | None:
        """Build tools list based on planning decision."""
        if delegate:
            # Only expose use_agent
            delegate_actions = [a for a in self.actions if a.name == "use_agent"]
            if not delegate_actions:
                return self._build_tools()  # fallback: no use_agent available
            return self._build_tools_from_actions(delegate_actions)
        else:
            # Expose everything except use_agent
            normal_actions = [a for a in self.actions if a.name != "use_agent"]
            return self._build_tools_from_actions(normal_actions)

    def _build_tools_from_actions(self, actions: list) -> list[dict] | None:
        """Convert a list of Action objects to OpenAI tools format."""
        if not actions:
            return None
        import json as _json
        tools = []
        for action in actions:
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
        action_history: list[ActionRecord] = []
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]

        if context is not None:
            messages.append({"role": "user", "content": context})

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

            # Planning step: decide delegate or act directly
            has_use_agent = any(a.name == "use_agent" for a in self.actions)
            if has_use_agent:
                delegate, delegate_task = self._planning_step(messages, iterations)
            else:
                delegate = False
                delegate_task = ""

            # If delegating, call HiringManager directly (no LLM call needed)
            if delegate and delegate_task:
                iterations += 1
                use_agent_action = self._actions_by_name.get("use_agent")
                if use_agent_action:
                    logger.info(
                        "  [iter %d] use_agent(task=%s) (delegated by planner)",
                        iterations, delegate_task,
                    )
                    # Build truncated parent context for sub-agent
                    parent_context = self._build_parent_context_summary(messages)
                    result = use_agent_action.execute(
                        task=delegate_task,
                        parent_context=parent_context,
                    )
                    logger.info("    → %s", result if result else "(empty)")

                    record = ActionRecord(
                        action_name="use_agent",
                        arguments={"task": delegate_task},
                        result=result,
                        timestamp=time.time(),
                    )
                    action_history.append(record)

                    # Inject as assistant + tool messages so LLM sees the result
                    messages.append({
                        "role": "assistant",
                        "content": f"I delegated to a sub-agent: {delegate_task}",
                    })
                    messages.append({
                        "role": "user",
                        "content": f"Sub-agent result:\n{result}",
                    })
                    continue

            # Normal action: expose all tools except use_agent
            tools = self._build_tools_filtered(False) if has_use_agent else self._build_tools()

            try:
                request = LLMRequest(messages=messages, model="default", tools=tools)
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
                        logger.warning("  [iter %d] Unknown tool: %s", iterations, tool_call.name)
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
                        ", ".join(f"{k}={repr(v)}" for k, v in tool_call.arguments.items()),
                        resp_tokens,
                    )
                    result = action.execute(**tool_call.arguments)
                    logger.info("    → %s", result if result else "(empty)")

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
                        iterations, resp_tokens, response.content,
                    )
                    messages.append({"role": "assistant", "content": response.content})
                    continue

                logger.info(
                    "  [iter %d] Text response (no tool call, %d tokens): %s",
                    iterations, resp_tokens, response.content,
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
