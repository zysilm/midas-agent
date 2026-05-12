"""Unit tests for SWEBenchScorer.

Tests the scorer's behavior:
- Empty patch returns 0.0 (no evaluation needed)
- _parse_report correctly converts swebench reports to s_exec
- Unique run_id per scorer instance (no stale cache)
- No silent fallbacks — exceptions propagate
- score() calls official run_instance (mocked)
"""
import pytest
from unittest.mock import patch, MagicMock

from llm_agent_toolkit.evaluation.swebench_scorer import SWEBenchScorer
from llm_agent_toolkit.types import Issue


def _make_issue(**kwargs) -> Issue:
    defaults = dict(
        issue_id="django__django-16379",
        repo="django/django",
        description="Fix bug",
        base_commit="abc123",
        fail_to_pass=["test_a", "test_b"],
        pass_to_pass=["test_c"],
    )
    defaults.update(kwargs)
    return Issue(**defaults)


@pytest.mark.unit
class TestSWEBenchScorer:
    def test_inherits_from_execution_scorer(self):
        from llm_agent_toolkit.evaluation.execution_scorer import ExecutionScorer
        scorer = SWEBenchScorer()
        assert isinstance(scorer, ExecutionScorer)

    def test_empty_patch_returns_zero(self):
        scorer = SWEBenchScorer()
        assert scorer.score("", _make_issue()) == 0.0
        assert scorer.score("   ", _make_issue()) == 0.0

    def test_unique_run_id_per_instance(self):
        """Each SWEBenchScorer gets a unique run_id to prevent stale cache."""
        scorer1 = SWEBenchScorer()
        scorer2 = SWEBenchScorer()
        assert scorer1._run_id != scorer2._run_id
        assert scorer1._run_id.startswith("midas-")
        assert scorer2._run_id.startswith("midas-")

    def test_run_id_is_uuid_based(self):
        """run_id contains a UUID hex segment."""
        scorer = SWEBenchScorer()
        parts = scorer._run_id.split("-", 1)
        assert parts[0] == "midas"
        assert len(parts[1]) == 8  # hex[:8]


@pytest.mark.unit
class TestParseReport:
    """_parse_report converts swebench report dict to s_exec float."""

    def test_resolved(self):
        scorer = SWEBenchScorer()
        report = {"django__django-16379": {"resolved": True}}
        assert scorer._parse_report(report, _make_issue()) == 1.0

    def test_patch_not_applied(self):
        scorer = SWEBenchScorer()
        report = {"django__django-16379": {
            "patch_successfully_applied": False,
            "resolved": False,
        }}
        assert scorer._parse_report(report, _make_issue()) == 0.0

    def test_partial_pass(self):
        scorer = SWEBenchScorer()
        report = {"django__django-16379": {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "test_a": "PASSED",
                "test_b": "FAILED",
                "test_c": "PASSED",
            },
        }}
        assert scorer._parse_report(report, _make_issue()) == pytest.approx(0.5)

    def test_regression_returns_zero(self):
        scorer = SWEBenchScorer()
        report = {"django__django-16379": {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "test_a": "PASSED",
                "test_b": "PASSED",
                "test_c": "FAILED",  # PASS_TO_PASS regression
            },
        }}
        assert scorer._parse_report(report, _make_issue()) == 0.0

    def test_all_fail(self):
        scorer = SWEBenchScorer()
        report = {"django__django-16379": {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "test_a": "FAILED",
                "test_b": "FAILED",
                "test_c": "PASSED",
            },
        }}
        assert scorer._parse_report(report, _make_issue()) == 0.0

    def test_no_fail_to_pass(self):
        scorer = SWEBenchScorer()
        report = {"django__django-16379": {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {},
        }}
        issue = _make_issue(fail_to_pass=[])
        assert scorer._parse_report(report, issue) == 0.0


@pytest.mark.unit
class TestScoreCallsRunInstance:
    """score() delegates to official run_instance — no reimplementation."""

    def test_score_calls_run_instance(self):
        """score() calls swebench's run_instance, not our own Docker code."""
        scorer = SWEBenchScorer()
        issue = _make_issue()

        mock_report = {issue.issue_id: {"resolved": True}}

        with patch("midas_agent.evaluation.swebench_scorer.run_instance") as mock_run, \
             patch("midas_agent.evaluation.swebench_scorer.make_test_spec") as mock_spec, \
             patch("midas_agent.evaluation.swebench_scorer.docker") as mock_docker, \
             patch.object(scorer, "_load_instance", return_value={"instance_id": issue.issue_id}):

            mock_run.return_value = (issue.issue_id, mock_report)
            mock_spec.return_value = MagicMock()
            mock_docker.from_env.return_value = MagicMock()

            result = scorer.score("diff --git a/fix.py\n+fixed", issue)

        assert result == 1.0
        mock_run.assert_called_once()

    def test_score_raises_on_none_result(self):
        """score() raises RuntimeError when run_instance returns None."""
        scorer = SWEBenchScorer()
        issue = _make_issue()

        with patch("midas_agent.evaluation.swebench_scorer.run_instance") as mock_run, \
             patch("midas_agent.evaluation.swebench_scorer.make_test_spec") as mock_spec, \
             patch("midas_agent.evaluation.swebench_scorer.docker") as mock_docker, \
             patch.object(scorer, "_load_instance", return_value={"instance_id": issue.issue_id}):

            mock_run.return_value = None
            mock_spec.return_value = MagicMock()
            mock_docker.from_env.return_value = MagicMock()

            with pytest.raises(RuntimeError, match="returned None"):
                scorer.score("diff --git a/fix.py\n+fixed", issue)

    def test_score_propagates_exceptions(self):
        """score() does NOT catch exceptions silently — they propagate."""
        scorer = SWEBenchScorer()
        issue = _make_issue()

        with patch("midas_agent.evaluation.swebench_scorer.run_instance") as mock_run, \
             patch("midas_agent.evaluation.swebench_scorer.make_test_spec") as mock_spec, \
             patch("midas_agent.evaluation.swebench_scorer.docker") as mock_docker, \
             patch.object(scorer, "_load_instance", return_value={"instance_id": issue.issue_id}):

            mock_run.side_effect = RuntimeError("Docker exploded")
            mock_spec.return_value = MagicMock()
            mock_docker.from_env.return_value = MagicMock()

            with pytest.raises(RuntimeError, match="Docker exploded"):
                scorer.score("diff --git a/fix.py\n+fixed", issue)

    def test_score_passes_unique_run_id(self):
        """score() passes the scorer's unique run_id to run_instance."""
        scorer = SWEBenchScorer()
        issue = _make_issue()

        mock_report = {issue.issue_id: {"resolved": True}}

        with patch("midas_agent.evaluation.swebench_scorer.run_instance") as mock_run, \
             patch("midas_agent.evaluation.swebench_scorer.make_test_spec") as mock_spec, \
             patch("midas_agent.evaluation.swebench_scorer.docker") as mock_docker, \
             patch.object(scorer, "_load_instance", return_value={"instance_id": issue.issue_id}):

            mock_run.return_value = (issue.issue_id, mock_report)
            mock_spec.return_value = MagicMock()
            mock_docker.from_env.return_value = MagicMock()

            scorer.score("diff --git a/fix.py\n+fixed", issue)

        call_kwargs = mock_run.call_args
        assert call_kwargs is not None
        # run_id should be our unique ID
        assert scorer._run_id in str(call_kwargs)

    def test_no_grade_output_method(self):
        """Scorer should NOT have _grade_output — we use official swebench."""
        scorer = SWEBenchScorer()
        assert not hasattr(scorer, "_grade_output"), \
            "_grade_output should be removed — use official run_instance"

    def test_no_run_and_grade_method(self):
        """Scorer should NOT have _run_and_grade — we use official swebench."""
        scorer = SWEBenchScorer()
        assert not hasattr(scorer, "_run_and_grade"), \
            "_run_and_grade should be removed — use official run_instance"
