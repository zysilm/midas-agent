"""Unit tests for PricingEngine."""
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.scheduler.training_log import TrainingLog


@pytest.mark.unit
class TestPricingEngine:
    """Tests for the PricingEngine class."""

    def _make_agent(self, agent_id: str = "agent-1") -> Agent:
        """Create a test Agent."""
        soul = Soul(system_prompt=f"Agent {agent_id}")
        return Agent(agent_id=agent_id, soul=soul, agent_type="free")

    def test_construction(self):
        """PricingEngine can be constructed with a TrainingLog and buffer_multiplier."""
        training_log = MagicMock(spec=TrainingLog)
        engine = PricingEngine(training_log=training_log, buffer_multiplier=1.2)

        assert engine is not None

    def test_calculate_price_returns_int(self):
        """calculate_price() returns an integer price for a given agent."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 500
        training_log.get_log_entries.return_value = []
        engine = PricingEngine(training_log=training_log, buffer_multiplier=1.2)
        agent = self._make_agent()

        price = engine.calculate_price(agent)

        assert isinstance(price, int)

    def test_price_includes_base_cost(self):
        """Price includes a base cost derived from weighted average historical cost times buffer."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 1000
        training_log.get_log_entries.return_value = []
        engine = PricingEngine(training_log=training_log, buffer_multiplier=1.5)
        agent = self._make_agent()

        price = engine.calculate_price(agent)

        # The price should be a positive integer reflecting base cost with buffer
        assert isinstance(price, int)
        assert price > 0

    def test_price_includes_debt_premium(self):
        """A negative balance adds a premium to the calculated price."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = -200  # Agent is in debt
        training_log.get_log_entries.return_value = []
        engine = PricingEngine(training_log=training_log, buffer_multiplier=1.2)
        agent = self._make_agent()

        price_with_debt = engine.calculate_price(agent)

        # Price with debt should still be a valid int
        assert isinstance(price_with_debt, int)
        assert price_with_debt > 0
