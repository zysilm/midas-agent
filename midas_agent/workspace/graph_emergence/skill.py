"""Skill and SkillReviewer for Graph Emergence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from midas_agent.llm.types import LLMRequest, LLMResponse

if TYPE_CHECKING:
    from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager


@dataclass
class Skill:
    name: str
    description: str
    content: str  # hard limit 5000 chars


class SkillReviewer:
    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
        free_agent_manager: FreeAgentManager,
    ) -> None:
        raise NotImplementedError

    def review(self, eval_results: dict) -> None:
        raise NotImplementedError
