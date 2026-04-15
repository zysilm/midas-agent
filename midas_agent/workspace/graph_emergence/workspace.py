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
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.types import Issue
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.graph_emergence.agent import Agent
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
    ) -> None:
        super().__init__(workspace_id, call_llm, system_llm)
        self._responsible_agent = responsible_agent
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._free_agent_manager = free_agent_manager
        self._skill_reviewer = skill_reviewer
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
        bash = BashAction(cwd=self.work_dir or None)
        search = SearchCodeAction(cwd=self.work_dir or None)
        find = FindFilesAction(cwd=self.work_dir or None)

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: self._free_agent_manager.match(desc),
        )

        actions = [
            bash,
            ReadFileAction(),
            EditFileAction(),
            WriteFileAction(),
            search,
            find,
            TaskDoneAction(),
            delegate,
        ]

        agent = PlanExecuteAgent(
            system_prompt=self._responsible_agent.soul.system_prompt,
            actions=actions,
            call_llm=self._call_llm,
            max_iterations=50,
            market_info_provider=lambda: "budget info",
        )
        self._last_result = agent.run(context=issue.description)

    def submit_patch(self) -> None:
        self.calls.append(("submit_patch", {}))

        patches_dir = os.path.join(self._patches_dir, self.workspace_id)
        os.makedirs(patches_dir, exist_ok=True)

        patch_content = self._generate_patch()
        episode_id = uuid.uuid4().hex[:8]
        patch_path = os.path.join(patches_dir, f"{episode_id}.patch")
        with open(patch_path, "w") as f:
            f.write(patch_content)

    def _generate_patch(self) -> str:
        """Get patch content from git diff if work_dir is set."""
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
        return ""

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> None:
        self.calls.append(("post_episode", {"eval_results": eval_results, "evicted_ids": evicted_ids}))
        self._skill_reviewer.review(eval_results)
        return None
