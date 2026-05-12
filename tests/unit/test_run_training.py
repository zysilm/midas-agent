"""Unit tests for run_training entry point."""
from unittest.mock import patch, MagicMock

import pytest

from midas_agent.training import run_training, collect_patches, load_swe_bench
from midas_agent.config import MidasConfig
from llm_agent_toolkit.llm.types import LLMResponse, TokenUsage, ToolCall
from llm_agent_toolkit.types import Issue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USAGE = TokenUsage(input_tokens=100, output_tokens=50)


def _fake_provider():
    """Return a mock LLM provider that does bash search → task_done."""
    responses = [
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "grep -rn bug ."})],
            usage=_USAGE,
        ),
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c2", name="task_done", arguments={"result": "Fixed."})],
            usage=_USAGE,
        ),
    ]
    idx = {"i": 0}

    def complete(request):
        i = idx["i"]
        idx["i"] = (i + 1) % len(responses)  # cycle through responses
        return responses[i]

    provider = MagicMock()
    provider.complete = complete
    return provider


@pytest.mark.unit
class TestRunTraining:
    """Tests for the run_training() orchestration function."""

    def _make_config(self, **kwargs) -> MidasConfig:
        defaults = dict(
            initial_budget=10000,
            workspace_count=1,
            runtime_mode="config_evolution",
            model="fake-model",
            api_key="fake-key",
        )
        defaults.update(kwargs)
        return MidasConfig(**defaults)

    def _make_issues(self, count: int = 2) -> list[Issue]:
        return [
            Issue(
                issue_id=f"test-issue-{i}",
                repo="",
                description=f"Fix bug {i}",
            )
            for i in range(count)
        ]

    def test_run_training_callable(self):
        """run_training is a callable function."""
        assert callable(run_training)

    def test_run_training_accepts_config(self):
        """run_training accepts a MidasConfig argument and completes with no issues."""
        config = self._make_config()
        with patch("midas_agent.training._make_llm_provider", return_value=_fake_provider()):
            result = run_training(config, issues=[])
        assert result is None

    def test_run_training_creates_scheduler(self):
        """run_training must internally create a Scheduler to orchestrate episodes."""
        config = self._make_config()
        with patch("midas_agent.training.Scheduler") as MockScheduler, \
             patch("midas_agent.training._make_llm_provider", return_value=_fake_provider()):
            mock_instance = MockScheduler.return_value
            mock_instance.get_workspaces.return_value = []
            mock_instance.create_workspaces.return_value = None
            mock_instance.allocate_budgets.return_value = None
            mock_instance.evaluate_and_select.return_value = ([], [], {})
            mock_instance.replace_evicted.return_value = None

            run_training(config, issues=self._make_issues(1))
            MockScheduler.assert_called_once()

    def test_run_training_episode_loop(self):
        """run_training runs one episode per issue."""
        config = self._make_config()
        issues = self._make_issues(2)
        with patch("midas_agent.training.Scheduler") as MockScheduler, \
             patch("midas_agent.training._make_llm_provider", return_value=_fake_provider()):
            mock_instance = MockScheduler.return_value
            mock_instance.get_workspaces.return_value = []
            mock_instance.create_workspaces.return_value = None
            mock_instance.allocate_budgets.return_value = None
            mock_instance.evaluate_and_select.return_value = ([], [], {})
            mock_instance.replace_evicted.return_value = None

            run_training(config, issues=issues)

            mock_instance.create_workspaces.assert_called_once()
            assert mock_instance.allocate_budgets.call_count == 2

    def test_run_training_returns_none(self):
        """run_training should return None after completing all episodes."""
        config = self._make_config()
        with patch("midas_agent.training._make_llm_provider", return_value=_fake_provider()):
            result = run_training(config, issues=[])
        assert result is None

    def test_run_training_empty_issues_no_episodes(self):
        """With no issues, no episodes are executed."""
        config = self._make_config()
        with patch("midas_agent.training.Scheduler") as MockScheduler, \
             patch("midas_agent.training._make_llm_provider", return_value=_fake_provider()):
            mock_instance = MockScheduler.return_value
            mock_instance.create_workspaces.return_value = None

            run_training(config, issues=[])

            mock_instance.create_workspaces.assert_called_once()
            mock_instance.allocate_budgets.assert_not_called()

    def test_run_training_raises_without_model(self):
        """run_training raises ValueError when no model is configured."""
        config = MidasConfig(
            initial_budget=10000, workspace_count=1,
            runtime_mode="config_evolution",
        )
        with pytest.raises(ValueError, match="No LLM model configured"):
            run_training(config, issues=self._make_issues(1))

    def test_run_training_full_episode_with_mocked_llm(self):
        """Integration-style: run one episode with mocked LLM provider."""
        config = self._make_config(workspace_count=1)
        issues = self._make_issues(1)

        with patch("midas_agent.training._make_llm_provider", return_value=_fake_provider()), \
             patch("midas_agent.evaluation.swebench_scorer.SWEBenchScorer.score", return_value=0.0):
            run_training(config, issues=issues)


@pytest.mark.unit
class TestCollectPatches:
    def test_collect_empty_patch(self):
        ws = MagicMock()
        ws.workspace_id = "ws-0"
        ws._last_patch = ""
        patches = collect_patches([ws])
        assert patches == {"ws-0": ""}

    def test_collect_existing_patch(self):
        ws = MagicMock()
        ws.workspace_id = "ws-0"
        ws._last_patch = "diff --git a/foo"
        patches = collect_patches([ws])
        assert patches["ws-0"] == "diff --git a/foo"

    def test_collect_reads_from_workspace_not_disk(self, tmp_path):
        """collect_patches reads from _last_patch, not from disk."""
        ws_dir = tmp_path / "ws-0"
        ws_dir.mkdir()
        (ws_dir / "stale.patch").write_text("stale content")

        ws = MagicMock()
        ws.workspace_id = "ws-0"
        ws._last_patch = "fresh content"
        patches = collect_patches([ws], str(tmp_path))
        assert patches["ws-0"] == "fresh content"
