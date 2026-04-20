"""ConfigEvolutionWorkspace — Workspace implementation for Configuration Evolution."""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.types import Issue
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.config_evolution.config_creator import ConfigCreator
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig
from midas_agent.workspace.config_evolution.executor import DAGExecutor, ExecutionResult
from midas_agent.workspace.config_evolution.mutator import ConfigMutator, _config_to_yaml
from midas_agent.workspace.config_evolution.snapshot_store import ConfigSnapshot, ConfigSnapshotStore

logger = logging.getLogger(__name__)

# Step id used by the default single-step config (pre-first-success).
_DEFAULT_STEP_ID = "main"


class ConfigEvolutionWorkspace(Workspace):
    def __init__(
        self,
        workspace_id: str,
        workflow_config: WorkflowConfig,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
        dag_executor: DAGExecutor,
        config_mutator: ConfigMutator,
        config_creator: ConfigCreator,
        snapshot_store: ConfigSnapshotStore,
    ) -> None:
        super().__init__(workspace_id, call_llm, system_llm)
        self._workflow_config = workflow_config
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._dag_executor = dag_executor
        self._config_mutator = config_mutator
        self._config_creator = config_creator
        self._snapshot_store = snapshot_store
        self._budget = 0
        self._last_result: ExecutionResult | None = None
        self._episode_count = 0
        self._io = None  # Set by training.py for Docker execution mode

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_default_config(self) -> bool:
        """True when the workspace still has the initial single-step config."""
        return (
            len(self._workflow_config.steps) == 1
            and self._workflow_config.steps[0].id == _DEFAULT_STEP_ID
        )

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def receive_budget(self, amount: int) -> None:
        self._budget += amount
        self.budget_received += amount
        self.calls.append(("receive_budget", {"amount": amount}))

    def execute(self, issue: Issue) -> None:
        self.calls.append(("execute", {"issue_id": issue.issue_id}))
        if self._io is not None:
            self._dag_executor.set_io(self._io)
        if self.work_dir:
            self._dag_executor.set_work_dir(self.work_dir)
        self._last_result = self._dag_executor.execute(
            self._workflow_config, issue, self._call_llm,
        )

    def submit_patch(self) -> None:
        self.calls.append(("submit_patch", {}))

        patch_content = self._generate_patch()
        self._last_patch = patch_content

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

        episode_id = uuid.uuid4().hex[:8]
        patch_path = os.path.join(patches_dir, f"{episode_id}.patch")
        with open(patch_path, "w") as f:
            f.write(patch_content)

    def _generate_patch(self) -> str:
        """Get patch content from git diff — Docker IO first, then local, then DAG output."""
        if self._io is not None:
            try:
                self._io.run_bash("git add -A")
                result = self._io.run_bash("git diff --cached")
                self._io.run_bash("git reset")
                return result
            except Exception:
                pass
        if self.work_dir and os.path.isdir(os.path.join(self.work_dir, ".git")):
            try:
                result = subprocess.run(
                    ["git", "diff"],
                    cwd=self.work_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return result.stdout
            except Exception:
                pass
        # Fallback: use DAG execution output.
        if self._last_result is not None and self._last_result.patch:
            return self._last_result.patch
        return ""

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> dict | None:
        self.calls.append(("post_episode", {"eval_results": eval_results, "evicted_ids": evicted_ids}))

        # -- Config creation on first success --
        # If we still have the default single-step config and this episode
        # scored a perfect execution score, generate a real multi-step
        # config from the successful trace.
        my_results = eval_results.get(self.workspace_id, {})
        my_score = my_results.get("s_exec", 0.0)

        if (
            my_score >= 1.0
            and self._is_default_config()
            and self._last_result is not None
            and self._last_result.action_history
        ):
            generated = self._config_creator.create_config(
                action_history=self._last_result.action_history,
                score=my_score,
            )
            if generated is not None:
                self._workflow_config = generated
                logger.info(
                    "Workspace %s: created config '%s' (%d steps) from first success",
                    self.workspace_id,
                    generated.meta.name,
                    len(generated.steps),
                )
                return None  # survived, config upgraded

        # -- Normal post-episode flow --
        if self.workspace_id in evicted_ids:
            # Evicted: nothing to do.  The scheduler will replace this
            # workspace with a new one seeded from the best-η config.
            return None
        else:
            # Survived: reflective self-rewrite using real trace data.
            # Skip mutation for the default single-step config — it awaits
            # config creation on first success, not incremental mutation.
            has_trace = (
                self._last_result is not None
                and self._last_result.action_history
            )
            if not self._is_default_config() and has_trace:
                new_config = self._config_mutator.reflective_self_rewrite(
                    config=self._workflow_config,
                    action_history=self._last_result.action_history,
                    score=my_score,
                )
                self._workflow_config = new_config
            # else: keep current config as-is (default config or no trace)

        # -- Save snapshot for export --
        self._episode_count += 1
        my_s_w = my_results.get("s_w", 0.0)
        cost = max(1, self.budget_received)
        eta = my_s_w / cost if cost > 0 else 0.0
        self._snapshot_store.save(ConfigSnapshot(
            episode_id=f"ep-{self._episode_count}",
            workspace_id=self.workspace_id,
            config_yaml=_config_to_yaml(self._workflow_config),
            eta=eta,
            score=my_s_w,
            cost=cost,
            summary=f"s_exec={my_score}",
        ))

        return None
