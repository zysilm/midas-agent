"""Free agent manager — matching and lifecycle for free agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from midas_agent.scheduler.storage import LogFilter
from midas_agent.workspace.graph_emergence.pricing import PricingEngineBase

if TYPE_CHECKING:
    from midas_agent.scheduler.training_log import TrainingLog
    from midas_agent.workspace.graph_emergence.agent import Agent


def compute_bankruptcy_rate(
    agent_id: str,
    training_log: TrainingLog,
    evicted_ws_ids: set[str],
) -> float:
    """Compute service bankruptcy rate for a free agent.

    bankruptcy_rate = |{workspaces agent served that were evicted}|
                    / |{workspaces agent served}|

    Returns 0.0 when the agent has not served any workspace.
    """
    consume_entries = training_log.get_log_entries(
        LogFilter(entity_id=agent_id, type="consume")
    )
    served = {e.workspace_id for e in consume_entries if e.workspace_id}
    if not served:
        return 0.0
    return len(served & evicted_ws_ids) / len(served)


@dataclass
class Candidate:
    agent: Agent
    similarity: float
    price: int


class FreeAgentManager:
    def __init__(self, pricing_engine: PricingEngineBase) -> None:
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
        # If agent not found, just return (don't crash)
        if agent_id not in self._agents:
            return
        # For now, the word-overlap matching in match() already uses skill.description.
        # No additional embedding computation needed for the current matching heuristic.
