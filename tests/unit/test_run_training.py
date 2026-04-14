"""Unit tests for run_training entry point."""
from unittest.mock import patch, MagicMock

import pytest

from midas_agent.main_training import run_training
from midas_agent.config import MidasConfig


@pytest.mark.unit
class TestRunTraining:
    """Tests for the run_training() orchestration function."""

    def _make_config(self, **kwargs) -> MidasConfig:
        """Helper to create a MidasConfig with sensible defaults."""
        defaults = dict(
            initial_budget=10000,
            workspace_count=4,
            runtime_mode="config_evolution",
        )
        defaults.update(kwargs)
        return MidasConfig(**defaults)

    def test_run_training_callable(self):
        """run_training is a callable function."""
        assert callable(run_training)

    def test_run_training_accepts_config(self):
        """run_training accepts a MidasConfig argument and completes without error."""
        config = self._make_config()

        # Should complete without raising.
        result = run_training(config)
        assert result is None

    def test_run_training_creates_scheduler(self):
        """run_training must internally create a Scheduler to orchestrate episodes."""
        config = self._make_config()

        with patch("midas_agent.main_training.Scheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.get_workspaces.return_value = []
            mock_instance.create_workspaces.return_value = None
            mock_instance.allocate_budgets.return_value = None

            run_training(config)

            MockScheduler.assert_called_once()

    def test_run_training_episode_loop(self):
        """run_training must execute the 6-phase episode loop.

        The loop phases are:
        1. Allocate budgets
        2. Create/get workspaces
        3. Execute agent work
        4. Collect patches
        5. Evaluate and select
        6. Replace evicted workspaces
        """
        config = self._make_config()

        with patch("midas_agent.main_training.Scheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.get_workspaces.return_value = []
            mock_instance.create_workspaces.return_value = None
            mock_instance.allocate_budgets.return_value = None

            run_training(config)

            # Verify the core episode phases were invoked.
            mock_instance.create_workspaces.assert_called()
            mock_instance.allocate_budgets.assert_called()

    def test_run_training_returns_none(self):
        """run_training should return None after completing all episodes."""
        config = self._make_config()

        result = run_training(config)

        assert result is None
