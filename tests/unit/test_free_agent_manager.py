"""Unit tests for FreeAgentManager and Candidate."""
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.graph_emergence.free_agent_manager import (
    Candidate,
    FreeAgentManager,
)
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.pricing import PricingEngine


@pytest.mark.unit
class TestCandidate:
    """Tests for the Candidate data class."""

    def test_candidate_fields(self):
        """Candidate stores agent, similarity, and price correctly."""
        soul = Soul(system_prompt="Expert coder")
        agent = Agent(agent_id="free-1", soul=soul, agent_type="free")
        candidate = Candidate(agent=agent, similarity=0.92, price=150)

        assert candidate.agent is agent
        assert candidate.agent.agent_id == "free-1"
        assert candidate.similarity == 0.92
        assert candidate.price == 150


@pytest.mark.unit
class TestFreeAgentManager:
    """Tests for the FreeAgentManager class."""

    def _make_agent(self, agent_id: str = "free-1") -> Agent:
        """Create a test Agent."""
        soul = Soul(system_prompt=f"Agent {agent_id}")
        return Agent(agent_id=agent_id, soul=soul, agent_type="free")

    def test_construction(self):
        """FreeAgentManager can be constructed with a PricingEngine."""
        pricing_engine = MagicMock(spec=PricingEngine)
        manager = FreeAgentManager(pricing_engine=pricing_engine)

        assert manager is not None

    def test_register_agent(self):
        """register() adds an agent to the free_agents pool."""
        pricing_engine = MagicMock(spec=PricingEngine)
        manager = FreeAgentManager(pricing_engine=pricing_engine)
        agent = self._make_agent("free-1")

        manager.register(agent)

        assert "free-1" in manager.free_agents

    def test_match_returns_candidates(self):
        """match() returns a list of Candidate objects."""
        pricing_engine = MagicMock(spec=PricingEngine)
        pricing_engine.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing_engine)

        agent = self._make_agent("free-1")
        manager.register(agent)

        candidates = manager.match(task_description="Fix a Python bug")

        assert isinstance(candidates, list)
        assert all(isinstance(c, Candidate) for c in candidates)

    def test_match_respects_top_k(self):
        """match() returns at most top_k candidates."""
        pricing_engine = MagicMock(spec=PricingEngine)
        pricing_engine.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing_engine)

        for i in range(10):
            manager.register(self._make_agent(f"free-{i}"))

        candidates = manager.match(task_description="Fix a bug", top_k=3)

        assert isinstance(candidates, list)
        assert len(candidates) <= 3

    def test_update_embedding(self):
        """update_embedding() updates the index for a given agent."""
        pricing_engine = MagicMock(spec=PricingEngine)
        manager = FreeAgentManager(pricing_engine=pricing_engine)
        agent = self._make_agent("free-1")
        manager.register(agent)

        manager.update_embedding("free-1")  # Should not raise
