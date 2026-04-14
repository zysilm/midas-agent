"""Unit tests for LLMJudge."""
from unittest.mock import MagicMock

import pytest

from midas_agent.evaluation.llm_judge import LLMJudge
from midas_agent.types import Issue


@pytest.mark.unit
class TestLLMJudge:
    """Tests for the LLMJudge criteria-based evaluator."""

    def _make_issue(self, **kwargs) -> Issue:
        """Helper to create an Issue with sensible defaults."""
        defaults = dict(
            issue_id="ISSUE-200",
            repo="test/repo",
            description="Refactor the parser module for clarity",
            fail_to_pass=["test_parser"],
            pass_to_pass=["test_lexer"],
        )
        defaults.update(kwargs)
        return Issue(**defaults)

    def test_construction(self):
        """LLMJudge accepts an llm_provider and a criteria_cache."""
        llm_provider = MagicMock()
        criteria_cache = MagicMock()

        judge = LLMJudge(llm_provider=llm_provider, criteria_cache=criteria_cache)

        assert judge is not None

    def test_evaluate_returns_float(self):
        """evaluate(patch, issue) returns a float in [0, 1]."""
        llm_provider = MagicMock()
        criteria_cache = MagicMock()
        judge = LLMJudge(llm_provider=llm_provider, criteria_cache=criteria_cache)
        issue = self._make_issue()

        result = judge.evaluate("diff --git a/refactor.py ...", issue)

        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_evaluate_uses_criteria_cache(self):
        """evaluate() calls criteria_cache.get_or_extract to obtain criteria."""
        llm_provider = MagicMock()
        criteria_cache = MagicMock()
        criteria_cache.get_or_extract.return_value = [
            "Code is readable",
            "No unnecessary complexity",
        ]
        judge = LLMJudge(llm_provider=llm_provider, criteria_cache=criteria_cache)
        issue = self._make_issue()

        judge.evaluate("diff --git a/refactor.py ...", issue)

        criteria_cache.get_or_extract.assert_called_once()

    def test_evaluate_two_step_process(self):
        """evaluate() performs a two-step process: extract criteria, then evaluate each."""
        llm_provider = MagicMock()
        criteria_cache = MagicMock()
        criteria_list = ["Criterion A", "Criterion B", "Criterion C"]
        criteria_cache.get_or_extract.return_value = criteria_list
        judge = LLMJudge(llm_provider=llm_provider, criteria_cache=criteria_cache)
        issue = self._make_issue()

        judge.evaluate("diff --git a/refactor.py ...", issue)

        # The LLM provider should be called to evaluate against the criteria
        assert llm_provider.complete.call_count >= 1

    def test_evaluate_uses_own_provider(self):
        """evaluate() uses its own llm_provider, not a ResourceMeter-wrapped one."""
        own_provider = MagicMock()
        criteria_cache = MagicMock()
        criteria_cache.get_or_extract.return_value = ["Clarity"]
        judge = LLMJudge(llm_provider=own_provider, criteria_cache=criteria_cache)
        issue = self._make_issue()

        judge.evaluate("diff --git a/refactor.py ...", issue)

        # The judge's own provider must have been called (not some other provider)
        own_provider.complete.assert_called()
