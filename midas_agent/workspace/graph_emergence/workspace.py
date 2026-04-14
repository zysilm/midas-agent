"""GraphEmergenceWorkspace — Workspace implementation for Graph Emergence."""
from __future__ import annotations

from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
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
        raise NotImplementedError

    def receive_budget(self, amount: int) -> None:
        raise NotImplementedError

    def execute(self, issue: Issue) -> None:
        raise NotImplementedError

    def submit_patch(self) -> None:
        raise NotImplementedError

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> None:
        raise NotImplementedError
