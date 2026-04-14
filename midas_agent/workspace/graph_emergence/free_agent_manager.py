"""Free agent manager — matching and lifecycle for free agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from midas_agent.workspace.graph_emergence.pricing import PricingEngine

if TYPE_CHECKING:
    from midas_agent.workspace.graph_emergence.agent import Agent


@dataclass
class Candidate:
    agent: Agent
    similarity: float
    price: int


class FreeAgentManager:
    def __init__(self, pricing_engine: PricingEngine) -> None:
        raise NotImplementedError

    @property
    def free_agents(self) -> dict[str, Agent]:
        raise NotImplementedError

    def match(
        self,
        task_description: str,
        top_k: int = 5,
    ) -> list[Candidate]:
        raise NotImplementedError

    def register(self, agent: Agent) -> None:
        raise NotImplementedError

    def update_embedding(self, agent_id: str) -> None:
        raise NotImplementedError
