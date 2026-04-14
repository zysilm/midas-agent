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
        self._system_llm = system_llm
        self._free_agent_manager = free_agent_manager

    def review(self, eval_results: dict) -> None:
        if "agent_id" in eval_results:
            self._free_agent_manager.update_embedding(eval_results["agent_id"])
        # Optionally call system_llm for skill review
        self._system_llm(
            LLMRequest(
                messages=[
                    {
                        "role": "system",
                        "content": "Review the following evaluation results and suggest skill updates.",
                    },
                    {
                        "role": "user",
                        "content": str(eval_results),
                    },
                ],
                model="default",
            )
        )
