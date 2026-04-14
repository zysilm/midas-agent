"""Unit tests for SelectionEngine — bottom-n eviction logic.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError.
"""
import pytest

from midas_agent.scheduler.selection import SelectionEngine


@pytest.mark.unit
class TestSelectionEngine:
    """Tests for the SelectionEngine."""

    def test_construction_config_evolution(self):
        """SelectionEngine can be constructed in config_evolution mode."""
        engine = SelectionEngine("config_evolution", n_evict=1)
        assert engine is not None

    def test_construction_graph_emergence(self):
        """SelectionEngine can be constructed in graph_emergence mode."""
        engine = SelectionEngine("graph_emergence", n_evict=1)
        assert engine is not None

    def test_config_evolution_evicts_lowest(self):
        """In config_evolution mode, the workspace with the lowest eta is evicted."""
        engine = SelectionEngine("config_evolution", n_evict=1)
        etas = {"ws-1": 0.008, "ws-2": 0.002, "ws-3": 0.005}

        evicted, survivors = engine.run_selection(etas)

        assert "ws-2" in evicted
        assert len(evicted) == 1
        assert set(survivors) == {"ws-1", "ws-3"}

    def test_config_evolution_preserves_at_least_one(self):
        """Eviction never removes all workspaces; at least one must survive.

        When n_evict >= N, the engine evicts min(n_evict, N-1) instead.
        """
        engine = SelectionEngine("config_evolution", n_evict=5)
        etas = {"ws-1": 0.001, "ws-2": 0.002}

        evicted, survivors = engine.run_selection(etas)

        # Only 2 workspaces, so at most 1 can be evicted
        assert len(evicted) <= 1
        assert len(survivors) >= 1

    def test_graph_emergence_skips_eviction(self):
        """In graph_emergence mode, no workspaces are evicted."""
        engine = SelectionEngine("graph_emergence", n_evict=1)
        etas = {"ws-1": 0.001, "ws-2": 0.002, "ws-3": 0.005}

        evicted, survivors = engine.run_selection(etas)

        assert evicted == []
        assert set(survivors) == {"ws-1", "ws-2", "ws-3"}

    def test_config_evolution_tie_breaking(self):
        """When multiple workspaces have the same lowest eta, tie is broken (randomly).

        We verify that exactly n_evict workspaces are evicted even when
        there are ties. The specific choice among tied workspaces is
        non-deterministic.
        """
        engine = SelectionEngine("config_evolution", n_evict=1)
        # All three have the same eta
        etas = {"ws-1": 0.005, "ws-2": 0.005, "ws-3": 0.005}

        evicted, survivors = engine.run_selection(etas)

        assert len(evicted) == 1
        assert len(survivors) == 2
        assert evicted[0] in etas
