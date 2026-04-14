"""DAG executor for Configuration Evolution workflows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import ActionRegistry
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


class DAGExecutor:
    def __init__(self, action_registry: ActionRegistry) -> None:
        raise NotImplementedError

    def execute(
        self,
        config: WorkflowConfig,
        issue: Issue,
        call_llm: Callable[[LLMRequest], LLMResponse],
    ) -> ExecutionResult:
        raise NotImplementedError
