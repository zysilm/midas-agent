"""Frozen pricing engine for production mode."""
from __future__ import annotations

from typing import TYPE_CHECKING

from midas_agent.workspace.graph_emergence.pricing import PricingEngineBase

if TYPE_CHECKING:
    from midas_agent.workspace.graph_emergence.agent import Agent


class FrozenPricingEngine(PricingEngineBase):
    """Price lookup from a frozen dict. No TrainingLog dependency."""

    def __init__(self, prices: dict[str, int]) -> None:
        self._prices = dict(prices)

    def calculate_price(self, agent: Agent) -> int:
        return self._prices.get(agent.agent_id, 1)
