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
from midas_agent.workspace.config_evolution.config_creator import ConfigCreator, ConfigMerger
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig
from midas_agent.workspace.config_evolution.executor import DAGExecutor, ExecutionResult
from midas_agent.workspace.config_evolution.mutator import _config_to_yaml
from midas_agent.workspace.config_evolution.prompt_optimizer import GEPAConfigOptimizer
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
        prompt_optimizer: GEPAConfigOptimizer,
        config_creator: ConfigCreator,
        config_merger: ConfigMerger,
        snapshot_store: ConfigSnapshotStore,
    ) -> None:
        super().__init__(workspace_id, call_llm, system_llm)
        self._workflow_config = workflow_config
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._dag_executor = dag_executor
        self._prompt_optimizer = prompt_optimizer
        self._config_creator = config_creator
        self._config_merger = config_merger
        self._snapshot_store = snapshot_store
        self._budget = 0
        self._last_result: ExecutionResult | None = None
        self._last_issue: Issue | None = None
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

    def restore_state(self, config: WorkflowConfig, episode_count: int) -> None:
        """Restore workspace state from a checkpoint."""
        self._workflow_config = config
        self._episode_count = episode_count

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def receive_budget(self, amount: int) -> None:
        self._budget += amount
        self.budget_received += amount
        self.calls.append(("receive_budget", {"amount": amount}))

    def execute(self, issue: Issue) -> None:
        self.calls.append(("execute", {"issue_id": issue.issue_id}))
        self._last_issue = issue
        if self._io is not None:
            self._dag_executor.set_io(self._io)
            # Docker IO: use the container workdir, not the host path
            self._dag_executor.set_work_dir(self._io._workdir)
        elif self.work_dir:
            self._dag_executor.set_work_dir(self.work_dir)

        # Merge issue into config for multi-step DAGs.
        # The base config has generic prompts; the merged config has
        # issue-specific context embedded in each step prompt.
        if not self._is_default_config():
            exec_config = self._config_merger.merge(self._workflow_config, issue)
        else:
            exec_config = self._workflow_config

        self._last_result = self._dag_executor.execute(
            exec_config, issue, self._call_llm,
            balance_provider=lambda: self._budget,
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

        # -- Evaluate this episode --
        my_results = eval_results.get(self.workspace_id, {})
        my_score = my_results.get("s_exec", 0.0)
        has_trace = (
            self._last_result is not None
            and self._last_result.action_history
        )

        # -- Record successful episodes for GEPA (before config creation) --
        if (
            my_score >= 1.0
            and has_trace
            and self._last_issue is not None
        ):
            from midas_agent.workspace.config_evolution.config_creator import (
                format_trace,
            )
            full_trace = format_trace(self._last_result.action_history)
            self._prompt_optimizer.record_episode(
                task_input=self._last_issue.description,
                action_summary=full_trace,
                score=my_score,
                issue_id=self._last_issue.issue_id,
            )

        # -- Config creation on first success --
        # If we still have the default single-step config and this episode
        # scored a perfect execution score, generate a real multi-step
        # config from the successful trace.
        if (
            my_score >= 1.0
            and self._is_default_config()
            and has_trace
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
            # Survived: GEPA optimization (runs every N episodes when
            # enough data has accumulated).  Skip for the default
            # single-step config — it awaits config creation on first
            # success, not incremental mutation.
            self._last_gepa_changed = False
            if not self._is_default_config():
                new_config, changed = self._prompt_optimizer.maybe_optimize(
                    self._workflow_config,
                )
                self._workflow_config = new_config
                self._last_gepa_changed = changed
            # else: keep current config as-is (default config)

        # -- Save snapshot for export --
        self._episode_count += 1
        my_s_w = my_results.get("s_w", 0.0)
        cost = max(1, self.budget_received)
        eta = my_s_w / cost if cost > 0 else 0.0
        config_yaml = _config_to_yaml(self._workflow_config)
        self._snapshot_store.save(ConfigSnapshot(
            episode_id=f"ep-{self._episode_count}",
            workspace_id=self.workspace_id,
            config_yaml=config_yaml,
            eta=eta,
            score=my_s_w,
            cost=cost,
            summary=f"s_exec={my_score}",
        ))

        # -- Export config YAML to disk for observability --
        try:
            store_dir = getattr(self._snapshot_store, "store_dir", None)
            if store_dir:
                config_log_dir = os.path.join(os.path.dirname(store_dir), "configs")
            else:
                config_log_dir = "/tmp/midas_output/configs"
            os.makedirs(config_log_dir, exist_ok=True)
            path = os.path.join(config_log_dir, f"{self.workspace_id}_ep{self._episode_count}.yaml")
            with open(path, "w") as f:
                f.write(config_yaml)
        except Exception:
            pass

        return None
