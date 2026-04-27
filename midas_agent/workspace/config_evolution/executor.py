"""DAG executor for Configuration Evolution workflows.

Runs a single ReactAgent through all DAG steps sequentially.  When the
agent calls ``task_done`` at an intermediate step, the executor injects
the next step's prompt as a user message and continues the same
conversation.  Full context is preserved — no lossy forwarding.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import ActionRegistry
from midas_agent.stdlib.react_agent import ActionRecord, ReactAgent
from midas_agent.types import Issue
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig

logger = logging.getLogger(__name__)


class CyclicDependencyError(Exception):
    pass


@dataclass
class ExecutionResult:
    step_outputs: dict[str, str]
    patch: str | None
    aborted: bool
    abort_step: str | None
    action_history: list[ActionRecord] = field(default_factory=list)


class DAGExecutor:
    def __init__(
        self,
        action_registry: ActionRegistry,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        system_llm: Callable[[LLMRequest], LLMResponse] | None = None,
    ) -> None:
        self._action_registry = action_registry
        self._max_tool_output_chars = max_tool_output_chars
        self._max_context_tokens = max_context_tokens
        self._system_llm = system_llm

    def set_work_dir(self, work_dir: str) -> None:
        """Propagate working directory to all actions that support it."""
        for name in list(self._action_registry._actions):
            action = self._action_registry._actions[name]
            if hasattr(action, "cwd"):
                action.cwd = work_dir

    def set_io(self, io) -> None:
        """Propagate IO backend to all actions that support it."""
        for name in list(self._action_registry._actions):
            action = self._action_registry._actions[name]
            if hasattr(action, "_io"):
                action._io = io

    def execute(
        self,
        config: WorkflowConfig,
        issue: Issue,
        call_llm: Callable[[LLMRequest], LLMResponse],
        balance_provider: Callable[[], int] | None = None,
        lessons: list | None = None,
    ) -> ExecutionResult:
        # Validate DAG before execution.
        sorted_ids = self._topological_sort(config)
        steps_by_id = {step.id: step for step in config.steps}

        if not sorted_ids:
            return ExecutionResult(
                step_outputs={}, patch=None, aborted=True, abort_step=None,
            )

        # Single-step config: just run a plain ReactAgent (no phase injection).
        if len(sorted_ids) == 1:
            return self._execute_single_step(
                steps_by_id[sorted_ids[0]], issue, call_llm, balance_provider,
            )

        # Multi-step: run one continuous conversation with phase transitions.
        return self._execute_multi_step(
            sorted_ids, steps_by_id, issue, call_llm, balance_provider,
            lessons=lessons,
        )

    # ------------------------------------------------------------------
    # Single-step execution (plain ReactAgent, no phase injection)
    # ------------------------------------------------------------------

    def _execute_single_step(
        self,
        step,
        issue: Issue,
        call_llm: Callable,
        balance_provider: Callable[[], int] | None,
    ) -> ExecutionResult:
        actions = list(self._action_registry._actions.values())
        agent = ReactAgent(
            system_prompt=step.prompt,
            actions=actions,
            call_llm=call_llm,
            balance_provider=balance_provider,
            max_tool_output_chars=self._max_tool_output_chars,
            max_context_tokens=self._max_context_tokens,
            system_llm=self._system_llm,
        )

        try:
            result = agent.run(context=issue.description)
        except Exception:
            return ExecutionResult(
                step_outputs={}, patch=None, aborted=True, abort_step=step.id,
            )

        aborted = result.termination_reason == "budget_exhausted"
        return ExecutionResult(
            step_outputs={step.id: result.output},
            patch=result.output if not aborted else None,
            aborted=aborted,
            abort_step=step.id if aborted else None,
            action_history=result.action_history,
        )

    # ------------------------------------------------------------------
    # Multi-step execution (one conversation, phase transitions)
    # ------------------------------------------------------------------

    def _execute_multi_step(
        self,
        sorted_ids: list[str],
        steps_by_id: dict,
        issue: Issue,
        call_llm: Callable,
        balance_provider: Callable[[], int] | None,
        lessons: list | None = None,
    ) -> ExecutionResult:
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError
        from midas_agent.workspace.config_evolution.step_judge import StepJudge

        first_step = steps_by_id[sorted_ids[0]]
        actions = list(self._action_registry._actions.values())
        actions_by_name = {a.name: a for a in actions}

        # Remove task_done from DAG tools — judge handles step transitions
        dag_actions = [a for a in actions if a.name != "task_done"]
        dag_actions_by_name = {a.name: a for a in dag_actions}

        tools = ReactAgent(
            system_prompt="", actions=dag_actions, call_llm=call_llm,
        )._build_tools()

        # Step judge validates agent's completion claims
        judge = StepJudge(
            system_llm=self._system_llm,
        ) if self._system_llm else None

        # Build initial messages matching SWE-agent v1.0 architecture:
        # 1. System prompt (one-liner)
        # 2. Instance template (first user message with full issue)
        # 3. First step prompt
        from midas_agent.prompts import DAG_SYSTEM_PROMPT, DAG_INSTANCE_TEMPLATE

        system_prompt = DAG_SYSTEM_PROMPT
        if lessons:
            lesson_lines = ["\n\n## Lessons from similar past issues"]
            for lesson in lessons:
                lesson_lines.append(f"- Mistake: {lesson.mistake}")
                lesson_lines.append(f"  Lesson: {lesson.lesson}")
                if lesson.correct_approach:
                    lesson_lines.append(f"  Correct approach: {lesson.correct_approach}")
            system_prompt += "\n".join(lesson_lines)

        instance_msg = DAG_INSTANCE_TEMPLATE.format(
            issue_description=issue.description,
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instance_msg},
            {"role": "user", "content": (
                f"**Current phase: {first_step.id}**\n\n{first_step.prompt}"
            )},
        ]

        step_outputs: dict[str, str] = {}
        all_action_history: list[ActionRecord] = []
        step_action_start = 0  # index into all_action_history for current step
        current_step_idx = 0
        current_step_id = sorted_ids[0]
        current_step = first_step
        iterations = 0
        step_iterations = 0  # iterations within current step
        total_tokens = 0
        aborted = False
        abort_step: str | None = None

        logger.info("  [step 1/%d] %s", len(sorted_ids), current_step_id)

        def _advance_to_next_step(output_text: str) -> bool:
            """Advance to next step, keeping full conversation history.

            Returns True if there are more steps.
            """
            nonlocal current_step_idx, current_step_id, current_step
            nonlocal step_iterations, step_action_start, tools

            step_outputs[current_step_id] = output_text
            logger.info(
                "  [step %d/%d] %s done at iter %d (%d tokens).",
                current_step_idx + 1, len(sorted_ids),
                current_step_id, iterations, total_tokens,
            )

            current_step_idx += 1
            if current_step_idx >= len(sorted_ids):
                logger.info("  All %d steps complete.", len(sorted_ids))
                return False

            current_step_id = sorted_ids[current_step_idx]
            current_step = steps_by_id[current_step_id]
            step_iterations = 0
            step_action_start = len(all_action_history)

            logger.info(
                "  [step %d/%d] %s",
                current_step_idx + 1, len(sorted_ids), current_step_id,
            )

            # Rebuild tools (same set, no task_done)
            tools = ReactAgent(
                system_prompt="", actions=dag_actions, call_llm=call_llm,
            )._build_tools()

            # Inject next step prompt into the same conversation
            messages.append({
                "role": "user",
                "content": (
                    f"**Current phase: {current_step_id}**\n\n"
                    f"{current_step.prompt}\n\n"
                    f"Focus ONLY on this phase."
                ),
            })
            return True

        while True:
            # LLM call
            try:
                request = LLMRequest(messages=messages, model="default", tools=tools)
                response = call_llm(request)
            except BudgetExhaustedError:
                logger.info("  Budget exhausted at iter %d (%d tokens)", iterations + 1, total_tokens)
                aborted = True
                abort_step = current_step_id
                break

            iterations += 1
            step_iterations += 1
            if response.usage:
                total_tokens += response.usage.input_tokens + response.usage.output_tokens

            if response.tool_calls:
                # Build assistant message
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
                    action = dag_actions_by_name.get(tool_call.name)
                    if action is None:
                        available = ", ".join(sorted(dag_actions_by_name.keys()))
                        result = (
                            f"Error: tool '{tool_call.name}' is not available. "
                            f"Available tools: {available}"
                        )
                        logger.warning("  [iter %d] Unknown tool: %s", iterations, tool_call.name)
                    else:
                        logger.info(
                            "  [iter %d] %s(%s) (%d tokens)",
                            iterations, tool_call.name,
                            ", ".join(f"{k}={repr(v)[:80]}" for k, v in tool_call.arguments.items()),
                            total_tokens,
                        )
                        result = action.execute(**tool_call.arguments)
                        logger.info("    → %s", result[:200] if result else "(empty)")

                    # Truncate large output
                    if self._max_tool_output_chars and result and len(result) > self._max_tool_output_chars:
                        from midas_agent.context.truncation import truncate_output
                        result = truncate_output(result, max_chars=self._max_tool_output_chars)

                    all_action_history.append(ActionRecord(
                        action_name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                        timestamp=time.time(),
                    ))

                    # Add tool result
                    tool_content = result
                    if balance_provider is not None:
                        tool_content += f"\n[当前余额: {balance_provider()}]"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_content,
                    })

                # Compaction check
                if self._max_context_tokens and self._system_llm:
                    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
                    for m in messages:
                        for tc in m.get("tool_calls", []):
                            args = tc.get("function", {}).get("arguments", "")
                            total_chars += len(str(args))
                    total_tokens_est = total_chars // 4
                    from midas_agent.context.compaction import should_compact, build_compaction_prompt, build_compacted_history
                    if should_compact(total_tokens_est, self._max_context_tokens):
                        logger.info(
                            "  Context compaction triggered at %d tokens est (%d chars, %d messages)",
                            total_tokens_est, total_chars, len(messages),
                        )
                        compact_prompt = build_compaction_prompt(messages)
                        compact_request = LLMRequest(messages=compact_prompt, model="default")
                        compact_response = self._system_llm(compact_request)
                        summary = compact_response.content or ""
                        messages = build_compacted_history(messages, summary)
                        logger.info(
                            "  Compacted to %d messages (%d chars)",
                            len(messages), sum(len(str(m.get("content", ""))) for m in messages),
                        )
                        # Restore the original system prompt (with lessons)
                        if not messages or messages[0].get("role") != "system":
                            messages.insert(0, {"role": "system", "content": system_prompt})

                # Stuck detection
                if len(all_action_history) >= 3:
                    stuck_msg = ReactAgent._check_stuck(all_action_history)
                    if stuck_msg:
                        messages.append({"role": "user", "content": stuck_msg})

            elif response.content:
                # Text response = agent claims step is done.
                # Validate with judge (trust agent by default).
                logger.info("  [iter %d] Response (text): %s", iterations, response.content[:200])

                if judge and current_step.goal:
                    step_actions = all_action_history[step_action_start:]
                    trace_text = StepJudge.format_trace_for_judge(
                        step_actions, len(step_actions),
                    )
                    verdict = judge.validate_completion(
                        current_step.goal, trace_text, response.content,
                    )
                    logger.info(
                        "  [judge] step '%s': %s — %s",
                        current_step_id,
                        "ACCEPT" if verdict.done else "REJECT",
                        verdict.reason,
                    )

                    if verdict.done:
                        # Agent is done, advance
                        has_more = _advance_to_next_step(
                            response.content or verdict.reason,
                        )
                        if not has_more:
                            return ExecutionResult(
                                step_outputs=step_outputs,
                                patch=None,
                                aborted=False,
                                abort_step=None,
                                action_history=all_action_history,
                            )
                        continue
                    else:
                        # Judge rejected — agent skipped the step
                        # Tell agent to keep working
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append({
                            "role": "user",
                            "content": (
                                f"You haven't completed this step yet. "
                                f"The goal is: {current_step.goal}\n"
                                f"Please continue working on it."
                            ),
                        })
                        continue
                else:
                    # No judge or no goal — text response = step done
                    has_more = _advance_to_next_step(response.content)
                    if not has_more:
                        return ExecutionResult(
                            step_outputs=step_outputs,
                            patch=None,
                            aborted=False,
                            abort_step=None,
                            action_history=all_action_history,
                        )
                    continue

            else:
                # Empty response — terminate
                aborted = True
                abort_step = current_step_id
                break

        return ExecutionResult(
            step_outputs=step_outputs,
            patch=step_outputs.get(sorted_ids[-1]) if not aborted else None,
            aborted=aborted,
            abort_step=abort_step,
            action_history=all_action_history,
        )

    # ------------------------------------------------------------------
    # Topological sort
    # ------------------------------------------------------------------

    def _topological_sort(self, config: WorkflowConfig) -> list[str]:
        """Topologically sort the DAG steps using Kahn's algorithm."""
        step_ids = {step.id for step in config.steps}

        in_degree: dict[str, int] = {step.id: 0 for step in config.steps}
        dependents: dict[str, list[str]] = {step.id: [] for step in config.steps}

        for step in config.steps:
            for dep_id in step.inputs:
                if dep_id in step_ids:
                    in_degree[step.id] += 1
                    dependents[dep_id].append(step.id)

        queue: deque[str] = deque()
        for sid, deg in in_degree.items():
            if deg == 0:
                queue.append(sid)

        sorted_ids: list[str] = []
        while queue:
            current = queue.popleft()
            sorted_ids.append(current)
            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(sorted_ids) != len(step_ids):
            raise CyclicDependencyError(
                "Cyclic dependency detected among steps: "
                + ", ".join(sid for sid in step_ids if sid not in sorted_ids)
            )

        return sorted_ids
