"""Unit tests for EvaluationModule and EvalResult."""
import dataclasses
from unittest.mock import MagicMock

import pytest

from midas_agent.evaluation.module import EvaluationModule, EvalResult


@pytest.mark.unit
class TestEvalResult:
    """Tests for the EvalResult frozen data class."""

    def test_eval_result_fields(self):
        """EvalResult stores workspace_id, episode_id, s_exec, s_llm, and s_w."""
        result = EvalResult(
            workspace_id="ws-001",
            episode_id="ep-010",
            s_exec=0.75,
            s_llm=0.6,
            s_w=0.825,
        )

        assert result.workspace_id == "ws-001"
        assert result.episode_id == "ep-010"
        assert result.s_exec == 0.75
        assert result.s_llm == 0.6
        assert result.s_w == 0.825

    def test_eval_result_is_frozen(self):
        """Attempting to modify a frozen EvalResult raises FrozenInstanceError."""
        result = EvalResult(
            workspace_id="ws-001",
            episode_id="ep-010",
            s_exec=0.5,
            s_llm=0.4,
            s_w=0.56,
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            result.s_exec = 0.99  # type: ignore[misc]

    def test_eval_result_s_llm_none_when_perfect(self):
        """When s_exec is 1.0, s_llm should be None (LLM judge not needed)."""
        result = EvalResult(
            workspace_id="ws-002",
            episode_id="ep-020",
            s_exec=1.0,
            s_llm=None,
            s_w=1.0,
        )

        assert result.s_exec == 1.0
        assert result.s_llm is None
        assert result.s_w == 1.0


@pytest.mark.unit
class TestEvaluationModule:
    """Tests for the EvaluationModule facade."""

    def test_construction(self):
        """EvaluationModule accepts execution_scorer, llm_judge, and beta."""
        execution_scorer = MagicMock()
        llm_judge = MagicMock()

        module = EvaluationModule(
            execution_scorer=execution_scorer,
            llm_judge=llm_judge,
            beta=0.3,
        )

        assert module is not None

    def test_evaluate_all_returns_dict(self):
        """evaluate_all(patches) returns a dict mapping workspace_id to EvalResult."""
        execution_scorer = MagicMock()
        llm_judge = MagicMock()
        module = EvaluationModule(
            execution_scorer=execution_scorer,
            llm_judge=llm_judge,
            beta=0.3,
        )

        patches = {"ws-001": "diff --git a/foo.py ..."}
        result = module.evaluate_all(patches)

        assert isinstance(result, dict)
        for key, val in result.items():
            assert isinstance(key, str)
            assert isinstance(val, EvalResult)

    def test_evaluate_all_perfect_score_skips_llm(self):
        """When s_exec is 1.0, s_w must be 1.0 and LLMJudge must not be called."""
        execution_scorer = MagicMock()
        execution_scorer.score.return_value = 1.0
        llm_judge = MagicMock()

        module = EvaluationModule(
            execution_scorer=execution_scorer,
            llm_judge=llm_judge,
            beta=0.3,
        )

        patches = {"ws-001": "diff --git a/perfect.py ..."}
        results = module.evaluate_all(patches)

        llm_judge.evaluate.assert_not_called()
        result = results["ws-001"]
        assert result.s_exec == 1.0
        assert result.s_llm is None
        assert result.s_w == 1.0

    def test_evaluate_all_composite_score(self):
        """Composite score formula: s_w = s_exec + (1 - s_exec) * beta * s_llm."""
        s_exec = 0.6
        s_llm = 0.8
        beta = 0.3
        expected_s_w = s_exec + (1 - s_exec) * beta * s_llm

        execution_scorer = MagicMock()
        execution_scorer.score.return_value = s_exec
        llm_judge = MagicMock()
        llm_judge.evaluate.return_value = s_llm

        module = EvaluationModule(
            execution_scorer=execution_scorer,
            llm_judge=llm_judge,
            beta=beta,
        )

        patches = {"ws-001": "diff --git a/partial.py ..."}
        results = module.evaluate_all(patches)
        result = results["ws-001"]

        assert result.s_exec == pytest.approx(s_exec)
        assert result.s_llm == pytest.approx(s_llm)
        assert result.s_w == pytest.approx(expected_s_w)

    def test_get_score(self):
        """get_score(workspace_id, episode_id) returns a float score."""
        execution_scorer = MagicMock()
        llm_judge = MagicMock()
        module = EvaluationModule(
            execution_scorer=execution_scorer,
            llm_judge=llm_judge,
            beta=0.3,
        )

        score = module.get_score("ws-001", "ep-010")

        assert isinstance(score, float)
