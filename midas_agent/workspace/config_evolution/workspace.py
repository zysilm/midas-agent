"""ConfigEvolutionWorkspace — Workspace implementation for Configuration Evolution."""
from __future__ import annotations

from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.types import Issue
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig
from midas_agent.workspace.config_evolution.executor import DAGExecutor
from midas_agent.workspace.config_evolution.mutator import ConfigMutator
from midas_agent.workspace.config_evolution.snapshot_store import ConfigSnapshotStore


class ConfigEvolutionWorkspace(Workspace):
    def __init__(
        self,
        workspace_id: str,
        workflow_config: WorkflowConfig,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
        dag_executor: DAGExecutor,
        config_mutator: ConfigMutator,
        snapshot_store: ConfigSnapshotStore,
    ) -> None:
        raise NotImplementedError

    def receive_budget(self, amount: int) -> None:
        raise NotImplementedError

    def execute(self, issue: Issue) -> None:
        raise NotImplementedError

    def submit_patch(self) -> None:
        raise NotImplementedError

    def post_episode(self, eval_results: dict) -> dict | None:
        raise NotImplementedError
