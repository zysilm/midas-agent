"""HTAWorkspace — Workspace implementation for the HTA runtime mode.

Drives one issue through the HTA decision-graph engine, produces a patch from
the sandbox git diff, and — in post_episode — commits the episode's buffered
hypothesis advantages into the shared TypedAdvantageMemory, weighted by the
evaluation outcome.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.runtime.io_backend import IOBackend, LocalIO
from llm_agent_toolkit.stdlib.action import Action
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.base import Workspace
from midas_agent.workspace.hta.advantage_memory import TypedAdvantageMemory
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.engine import HTAEngine, HTAEngineConfig
from midas_agent.workspace.hta.graph import DecisionGraph

logger = logging.getLogger(__name__)


class HTAWorkspace(Workspace):
    def __init__(
        self,
        workspace_id: str,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
        actions: list[Action],
        advantage_memory: TypedAdvantageMemory,
        registry: DecisionPointRegistry,
        engine_config: HTAEngineConfig,
        train_dir: str,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        action_log=None,
    ) -> None:
        super().__init__(workspace_id, call_llm, system_llm)
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._actions = actions
        self._advantage_memory = advantage_memory
        self._registry = registry
        self._engine_config = engine_config
        self._train_dir = train_dir
        self._max_tool_output_chars = max_tool_output_chars
        self._max_context_tokens = max_context_tokens
        self._action_log = action_log

        self._budget = 0
        self._io: IOBackend | None = None   # set by training.py for Docker mode
        self._last_graph: DecisionGraph | None = None
        self._last_issue: Issue | None = None
        self._episode_count = 0

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def receive_budget(self, amount: int) -> None:
        self._budget += amount
        self.budget_received += amount
        self.calls.append(("receive_budget", {"amount": amount}))

    def execute(self, issue: Issue) -> None:
        self.calls.append(("execute", {"issue_id": issue.issue_id}))
        self._last_graph = None
        self._last_patch = ""
        self._last_issue = issue

        io, work_dir = self._resolve_io()
        self._propagate_io_to_actions(work_dir)

        engine = HTAEngine(
            issue=issue,
            call_llm=self._call_llm,
            system_llm=self._system_llm,
            actions=self._actions,
            advantage_memory=self._advantage_memory,
            registry=self._registry,
            run_bash=self._make_run_bash(io, work_dir),
            write_file=self._make_write_file(io, work_dir),
            remove_file=self._make_remove_file(io, work_dir),
            config=self._engine_config,
            work_dir=work_dir,
            balance_provider=lambda: self._budget,
            max_tool_output_chars=self._max_tool_output_chars,
            max_context_tokens=self._max_context_tokens,
            action_log=self._action_log,
        )
        try:
            self._last_graph = engine.run()
        except Exception as e:  # noqa: BLE001 — one issue must not abort training
            logger.error("HTAWorkspace %s: engine failed: %s", self.workspace_id, e)
            self._last_graph = None

    def submit_patch(self) -> None:
        self.calls.append(("submit_patch", {}))
        io, work_dir = self._resolve_io()
        patch = ""
        try:
            io.run_bash("git add -A", cwd=work_dir)
            patch = io.run_bash("git diff --cached", cwd=work_dir)
            io.run_bash("git reset", cwd=work_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("HTAWorkspace %s: patch extraction failed: %s", self.workspace_id, e)
        self._last_patch = patch
        self._write_patch_file(patch)

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> dict | None:
        self.calls.append((
            "post_episode", {"eval_results": eval_results, "evicted_ids": evicted_ids},
        ))
        self._episode_count += 1

        my_score = eval_results.get(self.workspace_id, {}).get("s_exec", 0.0)

        # Commit the episode's buffered hypothesis advantages — weighted by the
        # outcome — into the shared typed memory. A crashed episode (no graph)
        # discards its pending buffer instead, so memory is never poisoned.
        if self._last_graph is not None:
            self._advantage_memory.commit_pending(outcome_score=my_score)
        else:
            self._advantage_memory.discard_pending()

        self._export_graph()
        return None

    # ------------------------------------------------------------------
    # IO plumbing
    # ------------------------------------------------------------------

    def _resolve_io(self) -> tuple[IOBackend, str]:
        """Return the active IO backend and the working directory.

        Docker mode uses the container workdir; local mode uses work_dir.
        """
        if self._io is not None:
            return self._io, getattr(self._io, "_workdir", "/testbed")
        return LocalIO(), (self.work_dir or os.getcwd())

    def _propagate_io_to_actions(self, work_dir: str) -> None:
        """Point the execution-node actions at the sandbox (mirrors DAGExecutor)."""
        for action in self._actions:
            if self._io is not None and hasattr(action, "_io"):
                action._io = self._io
            if hasattr(action, "cwd"):
                action.cwd = work_dir

    @staticmethod
    def _abs(work_dir: str, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.join(work_dir, rel)

    def _make_run_bash(self, io: IOBackend, work_dir: str) -> Callable[[str], str]:
        return lambda cmd: io.run_bash(cmd, cwd=work_dir)

    def _make_write_file(self, io: IOBackend, work_dir: str) -> Callable[[str, str], str]:
        def write_file(rel: str, content: str) -> str:
            path = self._abs(work_dir, rel)
            io.write_file(path, content)
            return path
        return write_file

    def _make_remove_file(self, io: IOBackend, work_dir: str) -> Callable[[str], None]:
        def remove_file(rel: str) -> None:
            io.run_bash(f"rm -f {self._abs(work_dir, rel)}", cwd=work_dir)
        return remove_file

    # ------------------------------------------------------------------
    # Observability artifacts
    # ------------------------------------------------------------------

    def _write_patch_file(self, patch: str) -> None:
        try:
            patches_dir = os.path.join(
                self._train_dir, "log", "patches", self.workspace_id,
            )
            os.makedirs(patches_dir, exist_ok=True)
            episode_id = uuid.uuid4().hex[:8]
            with open(os.path.join(patches_dir, f"{episode_id}.patch"), "w") as f:
                f.write(patch)
        except Exception:  # noqa: BLE001 — observability only
            pass

    def _export_graph(self) -> None:
        if self._last_graph is None:
            return
        try:
            graph_dir = os.path.join(self._train_dir, "log", "hta_graphs")
            os.makedirs(graph_dir, exist_ok=True)
            path = os.path.join(
                graph_dir, f"{self.workspace_id}_ep{self._episode_count}.json",
            )
            with open(path, "w") as f:
                json.dump(self._last_graph.to_dict(), f, indent=2)
        except Exception:  # noqa: BLE001 — observability only
            pass
