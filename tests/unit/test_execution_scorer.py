"""Unit tests for ExecutionScorer."""
import pytest

from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.types import Issue


@pytest.mark.unit
class TestExecutionScorer:
    """Tests for the ExecutionScorer Docker-based deterministic scorer."""

    def _make_issue(self, **kwargs) -> Issue:
        """Helper to create an Issue with sensible defaults."""
        defaults = dict(
            issue_id="ISSUE-100",
            repo="test/repo",
            description="Fix the flaky test",
            fail_to_pass=["test_broken"],
            pass_to_pass=["test_stable"],
        )
        defaults.update(kwargs)
        return Issue(**defaults)

    def test_construction(self):
        """ExecutionScorer accepts docker_image and timeout parameters."""
        scorer = ExecutionScorer(docker_image="test:latest", timeout=300)

        assert scorer is not None

    def test_score_returns_float(self):
        """score(patch, issue) returns a float between 0.0 and 1.0."""
        scorer = ExecutionScorer(docker_image="test:latest", timeout=300)
        issue = self._make_issue()

        result = scorer.score("diff --git a/fix.py ...", issue)

        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_score_perfect_all_tests_pass(self):
        """When all FAIL_TO_PASS tests pass, score returns 1.0."""
        scorer = ExecutionScorer(docker_image="test:latest", timeout=300)
        issue = self._make_issue(
            fail_to_pass=["test_a", "test_b"],
            pass_to_pass=["test_c"],
        )

        # A perfect patch that fixes all failing tests
        result = scorer.score("diff --git a/perfect_fix.py ...", issue)

        assert result == 1.0

    def test_score_zero_on_regression(self):
        """When any PASS_TO_PASS test fails (regression), score is 0."""
        scorer = ExecutionScorer(docker_image="test:latest", timeout=300)
        issue = self._make_issue(
            fail_to_pass=["test_a"],
            pass_to_pass=["test_stable_1", "test_stable_2"],
        )

        # A patch that causes regression in passing tests
        result = scorer.score("diff --git a/regression.py ...", issue)

        assert result == 0.0

    def test_score_zero_on_apply_failure(self):
        """When the patch fails to apply, score is 0."""
        scorer = ExecutionScorer(docker_image="test:latest", timeout=300)
        issue = self._make_issue()

        # A malformed patch that cannot be applied
        result = scorer.score("not-a-valid-patch", issue)

        assert result == 0.0
