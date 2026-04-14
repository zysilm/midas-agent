"""Unit tests for FrozenPricingEngine."""
import pytest

from midas_agent.inference.frozen_pricing import FrozenPricingEngine
from midas_agent.workspace.graph_emergence.agent import Agent, Soul


@pytest.mark.unit
class TestFrozenPricingEngine:
    def _make_agent(self, agent_id: str = "fa-1") -> Agent:
        return Agent(agent_id=agent_id, soul=Soul(system_prompt="test"), agent_type="free")

    def test_returns_frozen_price(self):
        engine = FrozenPricingEngine({"fa-1": 2500, "fa-2": 800})
        agent = self._make_agent("fa-1")
        assert engine.calculate_price(agent) == 2500

    def test_returns_fallback_for_unknown_agent(self):
        engine = FrozenPricingEngine({"fa-1": 2500})
        agent = self._make_agent("fa-unknown")
        assert engine.calculate_price(agent) == 1

    def test_does_not_mutate_input(self):
        prices = {"fa-1": 100}
        engine = FrozenPricingEngine(prices)
        prices["fa-1"] = 999
        assert engine.calculate_price(self._make_agent("fa-1")) == 100

    def test_empty_prices(self):
        engine = FrozenPricingEngine({})
        assert engine.calculate_price(self._make_agent()) == 1
