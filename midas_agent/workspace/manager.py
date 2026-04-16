"""Workspace manager — lifecycle management for all workspaces."""
from __future__ import annotations

import glob
import os
from typing import Callable, IO, TYPE_CHECKING

from midas_agent.config import MidasConfig
from midas_agent.llm.types import LLMRequest, LLMResponse

if TYPE_CHECKING:
    from midas_agent.workspace.base import Workspace


class WorkspaceManager:
    def __init__(
        self,
        config: MidasConfig,
        call_llm_factory: Callable[[str], Callable],
        system_llm_callback: Callable[[LLMRequest], LLMResponse],
        action_log_dir: str = "/tmp/midas_action_logs",
    ) -> None:
        self._config = config
        self._call_llm_factory = call_llm_factory
        self._system_llm_callback = system_llm_callback
        self._workspaces: dict[str, Workspace] = {}
        self._action_log_dir = action_log_dir
        self._action_log_handles: dict[str, IO] = {}
        os.makedirs(self._action_log_dir, exist_ok=True)
        # Remove stale JSONL files from previous runs
        for stale in glob.glob(os.path.join(self._action_log_dir, "*.jsonl")):
            try:
                os.remove(stale)
            except OSError:
                pass

    @property
    def workspaces(self) -> dict[str, Workspace]:
        return dict(self._workspaces)

    def create(
        self,
        workspace_id: str,
        initial_config: dict | None = None,
    ) -> Workspace:
        call_llm = self._call_llm_factory(workspace_id)

        if self._config.runtime_mode == "graph_emergence":
            ws = self._create_graph_emergence_workspace(
                workspace_id, call_llm, initial_config,
            )
        else:
            # Default to config_evolution mode.
            ws = self._create_config_evolution_workspace(
                workspace_id, call_llm, initial_config,
            )

        self._workspaces[workspace_id] = ws
        return ws

    def destroy(self, workspace_id: str) -> None:
        self._close_action_log(workspace_id)
        self._workspaces.pop(workspace_id, None)

    def close_all_action_logs(self, remove_empty: bool = False) -> None:
        """Close all open action log file handles.

        Args:
            remove_empty: If True, delete JSONL files that are empty
                (0 bytes). This avoids leaving empty log files for
                workspaces that were created but never executed.
        """
        for ws_id in list(self._action_log_handles):
            handle = self._action_log_handles.get(ws_id)
            path = handle.name if handle and not handle.closed else None
            self._close_action_log(ws_id)
            if remove_empty and path:
                try:
                    if os.path.exists(path) and os.path.getsize(path) == 0:
                        os.remove(path)
                except OSError:
                    pass

    def _open_action_log(self, workspace_id: str) -> IO:
        """Open a JSONL action log file for a workspace."""
        log_path = os.path.join(self._action_log_dir, f"{workspace_id}.jsonl")
        handle = open(log_path, "w")
        self._action_log_handles[workspace_id] = handle
        return handle

    def _close_action_log(self, workspace_id: str) -> None:
        """Close the action log file handle for a workspace, if open."""
        handle = self._action_log_handles.pop(workspace_id, None)
        if handle is not None and not handle.closed:
            handle.flush()
            handle.close()

    def list_workspaces(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def replace(
        self,
        old_workspace_id: str,
        new_workspace_id: str,
        new_config: dict | None = None,
    ) -> Workspace:
        self.destroy(old_workspace_id)
        return self.create(new_workspace_id, initial_config=new_config)

    # ------------------------------------------------------------------
    # Private factory helpers
    # ------------------------------------------------------------------

    def _create_config_evolution_workspace(
        self,
        workspace_id: str,
        call_llm: Callable,
        initial_config: dict | None,
    ) -> Workspace:
        from midas_agent.stdlib.action import ActionRegistry
        from midas_agent.stdlib.actions.bash import BashAction
        from midas_agent.stdlib.actions.file_ops import (
            EditFileAction,
            ReadFileAction,
            WriteFileAction,
        )
        from midas_agent.stdlib.actions.search import FindFilesAction, SearchCodeAction
        from midas_agent.stdlib.actions.task_done import TaskDoneAction
        from midas_agent.workspace.config_evolution.config_schema import (
            ConfigMeta,
            StepConfig,
            WorkflowConfig,
        )
        from midas_agent.workspace.config_evolution.executor import DAGExecutor
        from midas_agent.workspace.config_evolution.mutator import ConfigMutator
        from midas_agent.workspace.config_evolution.snapshot_store import (
            ConfigSnapshotStore,
        )
        from midas_agent.workspace.config_evolution.workspace import (
            ConfigEvolutionWorkspace,
        )

        all_actions = [
            BashAction(),
            ReadFileAction(),
            EditFileAction(),
            WriteFileAction(),
            SearchCodeAction(),
            FindFilesAction(),
            TaskDoneAction(),
        ]
        registry = ActionRegistry(all_actions)
        dag_executor = DAGExecutor(action_registry=registry)
        mutator = ConfigMutator(system_llm=self._system_llm_callback)
        snapshot_store = ConfigSnapshotStore(store_dir="/tmp/midas_snapshots")

        # Build workflow config from initial_config or use a default.
        if initial_config and "meta" in initial_config and "steps" in initial_config:
            meta_data = initial_config["meta"]
            meta = ConfigMeta(
                name=meta_data.get("name", workspace_id),
                description=meta_data.get("description", "auto"),
            )
            steps = []
            for s in initial_config["steps"]:
                steps.append(StepConfig(
                    id=s.get("id", "main"),
                    prompt=s.get("prompt", "Solve the issue."),
                    tools=s.get("tools", []),
                    inputs=s.get("inputs", []),
                ))
            workflow_config = WorkflowConfig(meta=meta, steps=steps)
        else:
            workflow_config = WorkflowConfig(
                meta=ConfigMeta(name=workspace_id, description="auto"),
                steps=[StepConfig(id="main", prompt="Solve the issue.")],
            )

        return ConfigEvolutionWorkspace(
            workspace_id=workspace_id,
            workflow_config=workflow_config,
            call_llm=call_llm,
            system_llm=self._system_llm_callback,
            dag_executor=dag_executor,
            config_mutator=mutator,
            snapshot_store=snapshot_store,
        )

    def _create_graph_emergence_workspace(
        self,
        workspace_id: str,
        call_llm: Callable,
        initial_config: dict | None,
    ) -> Workspace:
        from midas_agent.scheduler.serial_queue import SerialQueue
        from midas_agent.scheduler.storage import InMemoryStorageBackend
        from midas_agent.scheduler.training_log import HookSet, TrainingLog
        from midas_agent.workspace.graph_emergence.agent import Agent, Soul
        from midas_agent.workspace.graph_emergence.free_agent_manager import (
            FreeAgentManager,
        )
        from midas_agent.workspace.graph_emergence.pricing import PricingEngine
        from midas_agent.workspace.graph_emergence.skill import SkillReviewer
        from midas_agent.workspace.graph_emergence.workspace import (
            GraphEmergenceWorkspace,
        )

        # Each graph emergence workspace gets its own lightweight training
        # log for the PricingEngine (pricing is per-workspace).
        storage = InMemoryStorageBackend()
        hooks = HookSet()
        queue = SerialQueue()
        training_log = TrainingLog(
            storage=storage, hooks=hooks, serial_queue=queue,
        )

        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)
        skill_reviewer = SkillReviewer(
            system_llm=self._system_llm_callback,
            free_agent_manager=free_agent_manager,
        )

        soul_prompt = (
            "You are a software engineer solving GitHub issues. Your goal is to make minimal "
            "changes to non-test source files that fix the issue described below.\n\n"
            "## Workflow\n"
            "Follow these steps in order:\n"
            "1. **Read the issue carefully.** Pay close attention to the full description, "
            "expected behavior, steps to reproduce, AND any hints or proposed fixes. If the "
            "issue suggests a specific format (e.g., an error message wording), follow it exactly.\n"
            "2. **Locate relevant code.** Use search_code and read_file to find the code "
            "responsible for the bug.\n"
            "3. **Write a reproduction script.** Create a small Python script that triggers the "
            "bug, run it with bash to confirm the failure.\n"
            "4. **Fix the code.** Make the minimal edit to the source file. Match the existing "
            "code style. Do NOT modify test files, setup.cfg, or pyproject.toml.\n"
            "5. **Verify the fix.** Re-run your reproduction script to confirm the bug is gone. "
            "Then run the relevant test suite with pytest to check for regressions.\n"
            "6. **Check edge cases.** Consider if your fix handles related scenarios correctly.\n\n"
            "## Rules\n"
            "- Do NOT modify any test files. Only change source code.\n"
            "- Keep changes minimal and focused — do not refactor unrelated code.\n"
            "- If the issue or hints propose a specific error message format, use that exact format.\n"
            "- You have a token budget. Every tool call costs tokens. Be efficient.\n\n"
            "## Delegation\n"
            "You have a tool called `use_agent` to spawn or hire sub-agents. Use it when:\n"
            "- A sub-task is independent and self-contained.\n"
            "- Your context is already long. A fresh agent has a clean context window.\n"
            "Do the work yourself when the next step depends on what you just learned.\n"
        )
        if initial_config and "system_prompt" in initial_config:
            soul_prompt = initial_config["system_prompt"]

        responsible_agent = Agent(
            agent_id=f"agent-{workspace_id}",
            soul=Soul(system_prompt=soul_prompt),
            agent_type="workspace_bound",
        )

        action_log = self._open_action_log(workspace_id)

        return GraphEmergenceWorkspace(
            workspace_id=workspace_id,
            responsible_agent=responsible_agent,
            call_llm=call_llm,
            system_llm=self._system_llm_callback,
            free_agent_manager=free_agent_manager,
            skill_reviewer=skill_reviewer,
            max_tool_output_chars=self._config.max_tool_output_chars,
            action_log=action_log,
        )
