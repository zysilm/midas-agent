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
        train_dir: str = "/tmp/midas_train",
    ) -> None:
        self._config = config
        self._call_llm_factory = call_llm_factory
        self._system_llm_callback = system_llm_callback
        self._workspaces: dict[str, Workspace] = {}
        self._train_dir = train_dir
        self._action_log_dir = os.path.join(train_dir, "log", "action_logs")
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
        from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
        from midas_agent.workspace.config_evolution.config_schema import (
            ConfigMeta,
            StepConfig,
            WorkflowConfig,
        )
        from midas_agent.workspace.config_evolution.config_creator import ConfigCreator, ConfigMerger
        from midas_agent.workspace.config_evolution.executor import DAGExecutor
        from midas_agent.workspace.config_evolution.prompt_optimizer import (
            GEPAConfigOptimizer,
        )
        from midas_agent.workspace.config_evolution.snapshot_store import (
            ConfigSnapshotStore,
        )
        from midas_agent.workspace.config_evolution.workspace import (
            ConfigEvolutionWorkspace,
        )

        all_actions = [
            BashAction(),
            StrReplaceEditorAction(),
        ]
        all_tool_names = [a.name for a in all_actions]
        registry = ActionRegistry(all_actions)
        dag_executor = DAGExecutor(
            action_registry=registry,
            max_tool_output_chars=self._config.max_tool_output_chars,
            max_context_tokens=self._config.max_context_tokens,
            system_llm=self._system_llm_callback,
        )
        prompt_optimizer = GEPAConfigOptimizer(
            system_llm=self._system_llm_callback,
            data_dir=os.path.join(self._train_dir, "data"),
        )
        config_creator = ConfigCreator(system_llm=self._system_llm_callback)
        config_merger = ConfigMerger(system_llm=self._system_llm_callback)
        snapshot_store = ConfigSnapshotStore(
            store_dir=os.path.join(self._train_dir, "log", "snapshots"),
        )

        from midas_agent.workspace.config_evolution.lesson_store import LessonStore
        lesson_store = LessonStore(
            store_path=os.path.join(self._train_dir, "data", "lessons.json"),
            similarity_threshold=self._config.lesson_similarity_threshold,
        )

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
            # Default: single-step general agent with full system prompt
            # and all tools.  This runs as a plain ReactAgent until the
            # first successful episode triggers config creation.
            from midas_agent.prompts import SYSTEM_PROMPT

            workflow_config = WorkflowConfig(
                meta=ConfigMeta(name=workspace_id, description="default single-step"),
                steps=[StepConfig(
                    id="main",
                    prompt=SYSTEM_PROMPT,
                    tools=all_tool_names,
                )],
            )

        return ConfigEvolutionWorkspace(
            workspace_id=workspace_id,
            workflow_config=workflow_config,
            call_llm=call_llm,
            system_llm=self._system_llm_callback,
            dag_executor=dag_executor,
            prompt_optimizer=prompt_optimizer,
            config_creator=config_creator,
            config_merger=config_merger,
            snapshot_store=snapshot_store,
            lesson_store=lesson_store,
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

        from midas_agent.prompts import SYSTEM_PROMPT
        soul_prompt = SYSTEM_PROMPT
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
