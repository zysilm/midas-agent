"""GraphEmergenceWorkspace — Workspace implementation for Graph Emergence."""
from __future__ import annotations

import os
import subprocess
import uuid
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.file_ops import (
    EditFileAction,
    ReadFileAction,
    WriteFileAction,
)
from midas_agent.stdlib.actions.search import FindFilesAction, SearchCodeAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.actions.think import ThinkAction
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.types import Issue
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
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
        # bash, read_file, edit_file, write_file, search_code, find_files,
        # task_done, delegate_task
        cwd = self.work_dir or None
        balance_provider = lambda: self._budget

        ov = self._action_overrides
        base_actions = [
            ov.get("bash", BashAction(cwd=cwd)),
            ov.get("read_file", ReadFileAction(cwd=cwd)),
            ov.get("edit_file", EditFileAction(cwd=cwd)),
            ov.get("write_file", WriteFileAction(cwd=cwd)),
            ov.get("search_code", SearchCodeAction(cwd=cwd)),
            ov.get("find_files", FindFilesAction(cwd=cwd)),
        ]

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: self._free_agent_manager.match(desc),
            spawn_callback=lambda desc: self._spawn_agent(desc),
            balance_provider=balance_provider,
            calling_agent_id=self._responsible_agent.agent_id,
            call_llm=self._call_llm,
            parent_actions=base_actions,
        )

        # Wire up get_diff for the review gate: use the active bash action
        # (Docker or local) to run git diff inside the working environment.
        bash_action = ov.get("bash", base_actions[0])
        def _get_diff() -> str:
            bash_action.execute(command="git add -A")
            diff = bash_action.execute(command="git diff --cached")
            bash_action.execute(command="git reset")
            return diff

        actions = list(self._extra_actions) + base_actions + [ThinkAction(), TaskDoneAction(get_diff=_get_diff), delegate]

        agent = PlanExecuteAgent(
            system_prompt=self._responsible_agent.soul.system_prompt,
            actions=actions,
            call_llm=self._call_llm,
            max_iterations=9999,
            market_info_provider=lambda: self._build_market_info(),
            balance_provider=balance_provider,
            max_tool_output_chars=self._max_tool_output_chars,
            action_log=self._action_log,
        )
        from midas_agent.prompts import TASK_PROMPT_TEMPLATE
        context = TASK_PROMPT_TEMPLATE.format(issue_description=issue.description)
        self._last_result = agent.run(context=context)

    def _build_market_info(self) -> str:
        """Build market info for the planning phase. Data only — guidance
        is in the system prompt."""
        lines = [f"Your balance: {self._budget}"]

        agents = self._free_agent_manager.free_agents
        if agents:
            lines.append("")
            lines.append("Available agents:")
            for agent_id, agent in agents.items():
                price = self._free_agent_manager._pricing_engine.calculate_price(agent)
                skill_name = agent.skill.name if agent.skill else "general"
                protected = getattr(agent, "protected_by", None)
                label = " [幼年agent]" if protected == self._responsible_agent.agent_id else ""
                lines.append(f"  - {agent_id}: {skill_name} (price={price}){label}")

        return "\n".join(lines)

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
        # Try Docker mode first (all ops inside container)
        docker_bash = self._action_overrides.get("bash")
        if docker_bash is not None and hasattr(docker_bash, "_container_id"):
            try:
                docker_bash.execute(command="git add -A")
                result = docker_bash.execute(command="git diff --cached")
                docker_bash.execute(command="git reset")
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
                self._skill_reviewer.review(agent, ws_results, action_history)
        return None
