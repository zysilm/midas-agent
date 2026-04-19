"""GraphEmergenceWorkspace — Workspace implementation for Graph Emergence."""
from __future__ import annotations

import os
import subprocess
import uuid
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.actions.update_plan import UpdatePlanAction
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.types import Issue
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import (
    FreeAgentManager,
    compute_bankruptcy_rate,
)
from midas_agent.workspace.graph_emergence.skill import SkillReviewer


class GraphEmergenceWorkspace(Workspace):
    def __init__(
        self,
        workspace_id: str,
        responsible_agent: Agent,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
        free_agent_manager: FreeAgentManager,
        skill_reviewer: SkillReviewer,
        action_overrides: dict | None = None,
        max_tool_output_chars: int | None = None,
        extra_actions: list | None = None,
        action_log: "IO | None" = None,
        training_log: "object | None" = None,
        evicted_ws_ids: "set[str] | None" = None,
    ) -> None:
        super().__init__(workspace_id, call_llm, system_llm)
        self._responsible_agent = responsible_agent
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._free_agent_manager = free_agent_manager
        self._skill_reviewer = skill_reviewer
        self._action_overrides = action_overrides or {}
        self._max_tool_output_chars = max_tool_output_chars
        self._extra_actions = extra_actions or []
        self._action_log = action_log
        self._training_log = training_log
        self._evicted_ws_ids = evicted_ws_ids or set()
        self._io = None  # Set by training.py for Docker execution mode
        self._budget = 0
        self._last_result = None
        self._patches_dir: str = "/tmp/patches"

    def receive_budget(self, amount: int) -> None:
        self._budget += amount
        self.budget_received += amount
        self.calls.append(("receive_budget", {"amount": amount}))

    def execute(self, issue: Issue) -> None:
        self.calls.append(("execute", {"issue_id": issue.issue_id}))

        # Build responsible agent's action set:
        # bash, str_replace_editor, search_code, find_files,
        # task_done, delegate_task
        # In Docker mode, use /testbed as cwd (inside the container).
        # In local mode, use the local work_dir.
        cwd = "/testbed" if self._io is not None else (self.work_dir or None)
        balance_provider = lambda: self._budget

        ov = self._action_overrides
        io = self._io
        base_actions = [
            ov.get("bash", BashAction(cwd=cwd, io=io)),
            ov.get("str_replace_editor", StrReplaceEditorAction(cwd=cwd, io=io)),
        ]

        # Build environment context (replaces _build_market_info)
        agent_lines = []
        agents = self._free_agent_manager.free_agents
        for agent_id, agent in agents.items():
            price = self._free_agent_manager._pricing_engine.calculate_price(agent)
            skill_name = agent.skill.name if agent.skill else "general"
            if self._training_log is not None:
                br = compute_bankruptcy_rate(
                    agent_id, self._training_log, self._evicted_ws_ids,
                )
            else:
                br = 0.0
            agent_lines.append(
                f"{agent_id}: {skill_name} (price={price}, bankruptcy={br:.2f})"
            )

        delegate = DelegateTaskAction(
            call_llm=self._call_llm,
            parent_actions=base_actions,
            parent_system_prompt=self._responsible_agent.soul.system_prompt,
        )

        actions = list(self._extra_actions) + base_actions + [UpdatePlanAction(), TaskDoneAction(), delegate]

        agent = PlanExecuteAgent(
            system_prompt=self._responsible_agent.soul.system_prompt,
            actions=actions,
            call_llm=self._call_llm,
            max_iterations=9999,
            balance_provider=balance_provider,
            max_tool_output_chars=self._max_tool_output_chars,
            action_log=self._action_log,
            env_cwd="/testbed",
            env_agents=agent_lines,
        )
        from midas_agent.prompts import TASK_PROMPT_TEMPLATE
        context = TASK_PROMPT_TEMPLATE.format(issue_description=issue.description)
        self._last_result = agent.run(context=context)

    def _spawn_agent(self, task_description: str) -> Agent:
        """Spawn a new free agent with protection relationship."""
        import uuid

        agent_id = f"spawned-{uuid.uuid4().hex[:8]}"
        agent = Agent(
            agent_id=agent_id,
            soul=Soul(system_prompt=f"You are a specialist agent. Task: {task_description}"),
            agent_type="free",
            protected_by=self._responsible_agent.agent_id,
        )
        self._free_agent_manager.register(agent)
        return agent

    def submit_patch(self) -> None:
        self.calls.append(("submit_patch", {}))

        patch_content = self._generate_patch()
        self._last_patch = patch_content

        patches_dir = os.path.join(self._patches_dir, self.workspace_id)
        os.makedirs(patches_dir, exist_ok=True)

        episode_id = uuid.uuid4().hex[:8]
        patch_path = os.path.join(patches_dir, f"{episode_id}.patch")
        with open(patch_path, "w") as f:
            f.write(patch_content)

    def _generate_patch(self) -> str:
        """Get patch content from git diff.

        Stages all changes (including untracked files) to capture the
        full diff, then resets the index so the working tree is unchanged.

        Supports two modes:
        - Local: work_dir is set, runs git locally.
        - Docker: action_overrides has a "bash" DockerBashAction, runs git
          inside the container.
        """
        # Try IO backend mode first (Docker or other remote)
        if self._io is not None:
            try:
                self._io.run_bash("git add -A")
                result = self._io.run_bash("git diff --cached")
                self._io.run_bash("git reset")
                return result
            except Exception:
                pass

        # Local mode
        if self.work_dir and os.path.isdir(os.path.join(self.work_dir, ".git")):
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=self.work_dir,
                    capture_output=True,
                    timeout=10,
                )
                result = subprocess.run(
                    ["git", "diff", "--cached"],
                    cwd=self.work_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                patch = result.stdout
                subprocess.run(
                    ["git", "reset"],
                    cwd=self.work_dir,
                    capture_output=True,
                    timeout=10,
                )
                return patch
            except Exception:
                pass
        return ""

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> None:
        self.calls.append(("post_episode", {"eval_results": eval_results, "evicted_ids": evicted_ids}))
        action_history = []
        if self._last_result is not None and hasattr(self._last_result, "action_history"):
            action_history = self._last_result.action_history
        # Extract this workspace's results from the nested dict
        ws_results = eval_results.get(self.workspace_id, eval_results)
        self._skill_reviewer.review(self._responsible_agent, ws_results, action_history)
        # Also review free agents that participated (spawned by this workspace)
        for agent in self._free_agent_manager.free_agents.values():
            if getattr(agent, "protected_by", None) == self._responsible_agent.agent_id:
                sub_history = getattr(agent, "_last_action_history", [])
                self._skill_reviewer.review(agent, ws_results, sub_history)
        return None
