"""ConfigEvolutionWorkspace — Workspace implementation for Configuration Evolution."""
from __future__ import annotations

import os
import uuid
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.types import Issue
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig
from midas_agent.workspace.config_evolution.executor import DAGExecutor, ExecutionResult
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
        super().__init__(workspace_id, call_llm, system_llm)
        self._workflow_config = workflow_config
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._dag_executor = dag_executor
        self._config_mutator = config_mutator
        self._snapshot_store = snapshot_store
        self._budget = 0
        self._last_result: ExecutionResult | None = None

    def receive_budget(self, amount: int) -> None:
        self._budget += amount
        self.budget_received += amount
        self.calls.append(("receive_budget", {"amount": amount}))

    def execute(self, issue: Issue) -> None:
        self.calls.append(("execute", {"issue_id": issue.issue_id}))
        self._last_result = self._dag_executor.execute(
            self._workflow_config, issue, self._call_llm,
        )

    def submit_patch(self) -> None:
        self.calls.append(("submit_patch", {}))
        # Derive patches directory from the snapshot store's store_dir.
        # snapshot_store.store_dir is typically "{base}/snapshots", so
        # patches go to "{base}/patches/{workspace_id}/".
        store_dir = getattr(self._snapshot_store, "store_dir", None)
        if store_dir is None:
            return

        patches_dir = os.path.join(
            os.path.dirname(store_dir), "patches", self.workspace_id,
        )
        os.makedirs(patches_dir, exist_ok=True)

        patch_content = ""
        if self._last_result is not None and self._last_result.patch:
            patch_content = self._last_result.patch

        episode_id = uuid.uuid4().hex[:8]
        patch_path = os.path.join(patches_dir, f"{episode_id}.patch")
        with open(patch_path, "w") as f:
            f.write(patch_content)

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> dict | None:
        self.calls.append(("post_episode", {"eval_results": eval_results, "evicted_ids": evicted_ids}))
        if self.workspace_id in evicted_ids:
            # Evicted: reproduce a new config variant.
            result = self._config_mutator.reproduce(
                self._workflow_config, summaries=["evicted"],
            )
            if isinstance(result, dict):
                return result
            return {"reproduced": True}
        else:
            # Survived: self-rewrite the current config.
            new_config = self._config_mutator.self_rewrite(
                self._workflow_config, summary="survived",
            )
            self._workflow_config = new_config
            return None
