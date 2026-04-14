"""Unit tests for run_training entry point."""
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
        """run_training accepts a MidasConfig argument."""
        config = self._make_config()

        # The stub raises NotImplementedError, confirming it accepts config
        with pytest.raises(NotImplementedError):
            run_training(config)

    def test_run_training_creates_scheduler(self):
        """run_training must internally create a Scheduler to orchestrate episodes.

        When implemented, the function body should import and instantiate
        Scheduler. Currently the stub raises NotImplementedError before
        reaching that point.
        """
        config = self._make_config()

        # The stub raises before creating a Scheduler.
        # Once implemented, we verify Scheduler is instantiated.
        with pytest.raises(NotImplementedError):
            run_training(config)

        # Post-implementation this test will be updated to mock Scheduler
        # and assert it was called. For now, it must fail (red phase).
        # We explicitly fail to signal this test is not yet green.
        pytest.fail(
            "run_training does not yet create a Scheduler (stub raises NotImplementedError)"
        )

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

        with pytest.raises(NotImplementedError):
            run_training(config)

        # The stub does not execute any loop phases.
        pytest.fail(
            "run_training does not yet execute the episode loop (stub raises NotImplementedError)"
        )

    def test_run_training_returns_none(self):
        """run_training should return None after completing all episodes."""
        config = self._make_config()

        # The stub raises NotImplementedError instead of returning None
        result = run_training(config)

        assert result is None
