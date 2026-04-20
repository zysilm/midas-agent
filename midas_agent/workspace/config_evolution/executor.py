"""DAG executor for Configuration Evolution workflows."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import ActionRegistry
from midas_agent.stdlib.react_agent import ActionRecord, ReactAgent
from midas_agent.types import Issue
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig


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
    def __init__(self, action_registry: ActionRegistry) -> None:
        self._action_registry = action_registry

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
    ) -> ExecutionResult:
        steps_by_id = {step.id: step for step in config.steps}

        # Step 1: Build adjacency and detect cycles using Kahn's algorithm.
        sorted_ids = self._topological_sort(config)

        # Step 2: Execute each step in topological order.
        step_outputs: dict[str, str] = {}
        all_action_history: list[ActionRecord] = []
        aborted = False
        abort_step: str | None = None

        for step_id in sorted_ids:
            step = steps_by_id[step_id]

            # Build context: issue description + prior step outputs.
            context_parts = [issue.description]
            for dep_id in step.inputs:
                if dep_id in step_outputs:
                    context_parts.append(
                        f"[Output from step '{dep_id}']: {step_outputs[dep_id]}"
                    )
            context = "\n\n".join(context_parts)

            # Get action subset from registry.
            if step.tools:
                actions = self._action_registry.get_subset(step.tools)
            else:
                actions = []

            # Create a ReactAgent and run it.
            agent = ReactAgent(
                system_prompt=step.prompt,
                actions=actions,
                call_llm=call_llm,
                max_iterations=10,
            )

            try:
                result = agent.run(context=context)
            except Exception:
                # ReactAgent does not catch RuntimeError etc., so they
                # propagate here.  Treat any unhandled exception as an abort.
                aborted = True
                abort_step = step_id
                break

            all_action_history.extend(result.action_history)

            # Check if the agent was aborted due to budget exhaustion.
            if result.termination_reason == "budget_exhausted":
                aborted = True
                abort_step = step_id
                step_outputs[step_id] = result.output
                break

            step_outputs[step_id] = result.output

        # Build the final ExecutionResult.
        patch = step_outputs.get(sorted_ids[-1]) if not aborted else None
        return ExecutionResult(
            step_outputs=step_outputs,
            patch=patch,
            aborted=aborted,
            abort_step=abort_step,
            action_history=all_action_history,
        )

    def _topological_sort(self, config: WorkflowConfig) -> list[str]:
        """Topologically sort the DAG steps using Kahn's algorithm.

        Raises CyclicDependencyError if a cycle is detected.
        """
        step_ids = {step.id for step in config.steps}

        # Build in-degree map and adjacency list.
        in_degree: dict[str, int] = {step.id: 0 for step in config.steps}
        dependents: dict[str, list[str]] = {step.id: [] for step in config.steps}

        for step in config.steps:
            for dep_id in step.inputs:
                if dep_id in step_ids:
                    in_degree[step.id] += 1
                    dependents[dep_id].append(step.id)

        # Kahn's algorithm.
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
