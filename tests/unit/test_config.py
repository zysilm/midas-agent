"""Unit tests for MidasConfig data class."""
import dataclasses

import pytest

from midas_agent.config import MidasConfig


@pytest.mark.unit
class TestMidasConfig:
    """Tests for the MidasConfig frozen data class."""

    def test_config_required_fields(self):
        """Creating MidasConfig with required fields sets them correctly."""
        cfg = MidasConfig(
            initial_budget=10000,
            workspace_count=4,
            runtime_mode="config_evolution",
        )

        assert cfg.initial_budget == 10000
        assert cfg.workspace_count == 4
        assert cfg.runtime_mode == "config_evolution"

    def test_config_defaults(self):
        """MidasConfig defaults: score_floor=0.01, multiplier_mode='adaptive', etc."""
        cfg = MidasConfig(
            initial_budget=5000,
            workspace_count=2,
            runtime_mode="config_evolution",
        )

        assert cfg.score_floor == 0.01
        assert cfg.multiplier_mode == "adaptive"
        assert cfg.multiplier_init == 1.0
        assert cfg.er_target == 0.1
        assert cfg.cool_down == 0.05
        assert cfg.mult_min == 0.5
        assert cfg.mult_max == 50.0
        assert cfg.beta == 0.3
        assert cfg.eval_model == ""
        assert cfg.n_evict == 0
        assert cfg.max_iterations_free_agent == 50
        assert cfg.storage_backend == "sqlite"

    def test_config_frozen(self):
        """Attempting to modify a frozen MidasConfig raises FrozenInstanceError."""
        cfg = MidasConfig(
            initial_budget=1000,
            workspace_count=1,
            runtime_mode="config_evolution",
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.initial_budget = 9999  # type: ignore[misc]

    def test_config_evolution_mode(self):
        """MidasConfig with runtime_mode='config_evolution'."""
        cfg = MidasConfig(
            initial_budget=8000,
            workspace_count=3,
            runtime_mode="config_evolution",
        )

        assert cfg.runtime_mode == "config_evolution"

    def test_config_graph_emergence_mode(self):
        """MidasConfig with runtime_mode='graph_emergence'."""
        cfg = MidasConfig(
            initial_budget=12000,
            workspace_count=6,
            runtime_mode="graph_emergence",
        )

        assert cfg.runtime_mode == "graph_emergence"

    def test_config_all_fields(self):
        """Creating MidasConfig with every field explicitly set."""
        cfg = MidasConfig(
            initial_budget=20000,
            workspace_count=8,
            runtime_mode="graph_emergence",
            score_floor=0.05,
            multiplier_mode="adaptive",
            multiplier_init=1.5,
            er_target=0.1,
            cool_down=0.1,
            mult_min=0.2,
            mult_max=3.0,
            beta=0.5,
            eval_model="claude-3-opus",
            n_evict=2,
            max_iterations_free_agent=100,
            storage_backend="jsonl",
        )

        assert cfg.initial_budget == 20000
        assert cfg.workspace_count == 8
        assert cfg.runtime_mode == "graph_emergence"
        assert cfg.score_floor == 0.05
        assert cfg.multiplier_mode == "adaptive"
        assert cfg.multiplier_init == 1.5
        assert cfg.er_target == 0.1
        assert cfg.cool_down == 0.1
        assert cfg.mult_min == 0.2
        assert cfg.mult_max == 3.0
        assert cfg.beta == 0.5
        assert cfg.eval_model == "claude-3-opus"
        assert cfg.n_evict == 2
        assert cfg.max_iterations_free_agent == 100
        assert cfg.storage_backend == "jsonl"
