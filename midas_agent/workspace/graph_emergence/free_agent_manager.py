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
        self._pricing_engine = pricing_engine
        self._agents: dict[str, Agent] = {}

    @property
    def free_agents(self) -> dict[str, Agent]:
        return dict(self._agents)

    def match(
        self,
        task_description: str,
        top_k: int = 5,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for agent in self._agents.values():
            # Simple similarity heuristic: check if agent skill description
            # has any word overlap with the task description.
            similarity = 0.5
            if agent.skill is not None:
                task_words = set(task_description.lower().split())
                skill_words = set(agent.skill.description.lower().split())
                if task_words & skill_words:
                    similarity = 1.0
            price = self._pricing_engine.calculate_price(agent)
            candidates.append(Candidate(agent=agent, similarity=similarity, price=price))
        candidates.sort(key=lambda c: c.similarity, reverse=True)
        return candidates[:top_k]

    def register(self, agent: Agent) -> None:
        self._agents[agent.agent_id] = agent

    def update_embedding(self, agent_id: str) -> None:
        # Placeholder for actual embedding update logic.
        pass
