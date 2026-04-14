"""Integration Test Suite 7: Evaluation Module.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError. These tests define expected behavior for the
EvaluationModule + LLMJudge + CriteriaCache pipeline.

Components under test:
  - EvaluationModule (facade)
  - LLMJudge (criteria-based LLM evaluation)
  - CriteriaCache (per-issue criteria persistence)

Mocked:
  - ExecutionScorer  -> FakeExecutionScorer (conftest)
  - LLMProvider      -> FakeLLMProvider (conftest)
"""
from __future__ import annotations

import json
import os

import pytest

from midas_agent.evaluation.criteria_cache import CriteriaCache
from midas_agent.evaluation.llm_judge import LLMJudge
from midas_agent.evaluation.module import EvalResult, EvaluationModule
from midas_agent.llm.types import LLMResponse, TokenUsage
from midas_agent.types import Issue

from tests.integration.conftest import (
    FAKE_ISSUE,
    FakeExecutionScorer,
    FakeLLMProvider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _criteria_response() -> LLMResponse:
    """Scripted LLM response that returns criteria JSON."""
    return LLMResponse(
        content=json.dumps(["handles edge cases", "correct logic", "clean diff"]),
        tool_calls=None,
        usage=TokenUsage(input_tokens=20, output_tokens=30),
    )


def _evaluation_response(score: float = 0.7) -> LLMResponse:
    """Scripted LLM response that returns a numeric evaluation score."""
    return LLMResponse(
        content=str(score),
        tool_calls=None,
        usage=TokenUsage(input_tokens=15, output_tokens=5),
    )


def _build_evaluation_module(
    scores: dict[str, float],
    llm_responses: list[LLMResponse] | None = None,
    beta: float = 0.3,
    cache_dir: str = "/tmp/midas_test_criteria",
) -> tuple[EvaluationModule, FakeExecutionScorer, FakeLLMProvider, CriteriaCache]:
    """Construct a fully wired EvaluationModule with test doubles."""
    fake_scorer = FakeExecutionScorer(scores=scores)

    responses = llm_responses or [_criteria_response(), _evaluation_response()]
    fake_llm = FakeLLMProvider(responses=responses)

    criteria_cache = CriteriaCache(cache_dir=cache_dir)
    llm_judge = LLMJudge(llm_provider=fake_llm, criteria_cache=criteria_cache)
    module = EvaluationModule(
        execution_scorer=fake_scorer,
        llm_judge=llm_judge,
        beta=beta,
    )
    return module, fake_scorer, fake_llm, criteria_cache


# ===========================================================================
# Integration tests
# ===========================================================================


@pytest.mark.integration
class TestEvaluationModuleIntegration:
    """Suite 7: EvaluationModule + LLMJudge + CriteriaCache."""

    # -----------------------------------------------------------------------
    # IT-7.1: S_exec=1.0 skips LLMJudge
    # -----------------------------------------------------------------------

    def test_perfect_exec_score_skips_llm_judge(self, temp_dir, fake_issue):
        """When S_exec=1.0, the LLM judge should NOT be invoked.
        S_w must equal 1.0 and the LLM call count must be zero."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")
        module, scorer, llm, _ = _build_evaluation_module(
            scores={"ws1": 1.0},
            cache_dir=cache_dir,
        )

        patches = {"ws1": "patch content for ws1"}
        results = module.evaluate_all(patches)

        assert "ws1" in results
        result = results["ws1"]
        assert isinstance(result, EvalResult)
        assert result.s_exec == 1.0
        assert result.s_llm is None
        assert result.s_w == 1.0

        # LLM must not have been called at all
        assert llm.call_count == 0

    # -----------------------------------------------------------------------
    # IT-7.2: S_exec=0.5 triggers LLMJudge with correct formula
    # -----------------------------------------------------------------------

    def test_partial_exec_score_triggers_llm_judge(self, temp_dir, fake_issue):
        """When S_exec < 1.0, LLMJudge is invoked.
        Design formula: S_w = S_exec + (1 - S_exec) * beta * S_llm
        With S_exec=0.5, beta=0.3, S_llm=0.7:
        S_w = 0.5 + (1 - 0.5) * 0.3 * 0.7 = 0.5 + 0.105 = 0.605
        """
        cache_dir = os.path.join(temp_dir, "criteria_cache")
        module, scorer, llm, _ = _build_evaluation_module(
            scores={"ws1": 0.5},
            llm_responses=[_criteria_response(), _evaluation_response(0.7)],
            beta=0.3,
            cache_dir=cache_dir,
        )

        patches = {"ws1": "patch content for ws1"}
        results = module.evaluate_all(patches)

        result = results["ws1"]
        assert result.s_exec == 0.5
        assert result.s_llm == pytest.approx(0.7)
        # S_w = S_exec + (1 - S_exec) * beta * S_llm
        expected_sw = 0.5 + (1 - 0.5) * 0.3 * 0.7
        assert result.s_w == pytest.approx(expected_sw, rel=1e-6)

        # LLM was called (at least for criteria extraction and evaluation)
        assert llm.call_count >= 1

    # -----------------------------------------------------------------------
    # IT-7.3: S_exec=0.0 with S_llm=1.0 -- LLM cannot rescue zero execution
    # -----------------------------------------------------------------------

    def test_zero_exec_score_gets_llm_only_contribution(self, temp_dir, fake_issue):
        """S_exec=0.0, S_llm=1.0.
        Design formula: S_w = S_exec + (1 - S_exec) * beta * S_llm
        S_w = 0.0 + (1 - 0.0) * 0.3 * 1.0 = 0.3.
        LLM can contribute up to beta when S_exec=0 (design §2.4 W3 example)."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")
        module, scorer, llm, _ = _build_evaluation_module(
            scores={"ws1": 0.0},
            llm_responses=[_criteria_response(), _evaluation_response(1.0)],
            beta=0.3,
            cache_dir=cache_dir,
        )

        patches = {"ws1": "patch content for ws1"}
        results = module.evaluate_all(patches)

        result = results["ws1"]
        assert result.s_exec == 0.0
        # S_w = 0.0 + 1.0 * 0.3 * 1.0 = 0.3
        expected_sw = 0.0 + (1 - 0.0) * 0.3 * 1.0
        assert result.s_w == pytest.approx(expected_sw, rel=1e-6)

    # -----------------------------------------------------------------------
    # IT-7.4: CriteriaCache hit avoids re-extraction
    # -----------------------------------------------------------------------

    def test_criteria_cache_avoids_re_extraction(self, temp_dir, fake_issue):
        """Two evaluations for the same issue should only call the criteria
        extraction LLM once. The second evaluation reuses the cached criteria."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")

        # We need enough responses for two evaluations:
        # First eval:  criteria extraction (1 call) + evaluation (1 call)
        # Second eval:  criteria cached (0 calls) + evaluation (1 call)
        responses = [
            _criteria_response(),     # criteria extraction for first eval
            _evaluation_response(0.6),  # evaluation for first eval
            _evaluation_response(0.8),  # evaluation for second eval (no criteria call)
        ]

        fake_scorer = FakeExecutionScorer(scores={"ws1": 0.5, "ws2": 0.5})
        fake_llm = FakeLLMProvider(responses=responses)
        criteria_cache = CriteriaCache(cache_dir=cache_dir)
        llm_judge = LLMJudge(llm_provider=fake_llm, criteria_cache=criteria_cache)
        module = EvaluationModule(
            execution_scorer=fake_scorer,
            llm_judge=llm_judge,
            beta=0.3,
        )

        # First evaluation
        results1 = module.evaluate_all({"ws1": "patch content for ws1"})
        calls_after_first = fake_llm.call_count

        # Second evaluation -- same issue, different workspace
        results2 = module.evaluate_all({"ws2": "patch content for ws2"})
        calls_after_second = fake_llm.call_count

        # The criteria extraction call should happen only once (first eval).
        # The second eval should only add one evaluation call, not a criteria call.
        # Total calls: 2 (first eval) + 1 (second eval) = 3
        assert calls_after_first == 2, (
            f"First eval should use 2 LLM calls (criteria + eval), got {calls_after_first}"
        )
        assert calls_after_second == 3, (
            f"Second eval should add only 1 LLM call (eval only), got {calls_after_second}"
        )

    # -----------------------------------------------------------------------
    # IT-7.5: CriteriaCache persistence across instances
    # -----------------------------------------------------------------------

    def test_criteria_cache_persistence_across_instances(self, temp_dir):
        """Write criteria to cache, recreate CriteriaCache instance, verify
        the cached criteria are found without calling the extraction function."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")

        # First instance: populate the cache
        cache1 = CriteriaCache(cache_dir=cache_dir)
        extraction_call_count = 0

        def extract_fn(issue_id: str) -> list[str]:
            nonlocal extraction_call_count
            extraction_call_count += 1
            return ["criterion_a", "criterion_b", "criterion_c"]

        result1 = cache1.get_or_extract("issue-001", extract_fn)
        assert extraction_call_count == 1
        assert result1 == ["criterion_a", "criterion_b", "criterion_c"]

        # Second instance: same cache_dir, fresh object
        cache2 = CriteriaCache(cache_dir=cache_dir)
        result2 = cache2.get_or_extract("issue-001", extract_fn)

        # The extraction function must NOT have been called again
        assert extraction_call_count == 1, (
            "Criteria extraction should not be called for a cached issue"
        )
        assert result2 == ["criterion_a", "criterion_b", "criterion_c"]

    # -----------------------------------------------------------------------
    # IT-7.6: beta=0 produces pure execution score
    # -----------------------------------------------------------------------

    def test_beta_zero_pure_execution_score(self, temp_dir, fake_issue):
        """With beta=0, S_w = S_exec + (1 - S_exec) * 0 * S_llm = S_exec.
        The LLM judge should NOT be called because beta=0 makes it irrelevant."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")
        module, scorer, llm, _ = _build_evaluation_module(
            scores={"ws1": 0.5},
            beta=0.0,
            cache_dir=cache_dir,
        )

        patches = {"ws1": "patch content for ws1"}
        results = module.evaluate_all(patches)

        result = results["ws1"]
        assert result.s_exec == 0.5
        assert result.s_w == pytest.approx(0.5, rel=1e-6)

        # LLM should NOT be called when beta=0
        assert llm.call_count == 0

    # -----------------------------------------------------------------------
    # IT-7.7: No patch -> S_w=0
    # -----------------------------------------------------------------------

    def test_no_patch_yields_zero_score(self, temp_dir, fake_issue):
        """When a workspace produces no patch (empty string or missing),
        S_w should be 0. Neither the execution scorer nor the LLM judge
        should be invoked."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")
        module, scorer, llm, _ = _build_evaluation_module(
            scores={"ws_empty": 0.0},
            cache_dir=cache_dir,
        )

        # Empty patches dict -- no workspace produced a patch
        patches: dict[str, str] = {}
        results = module.evaluate_all(patches)

        assert results == {} or all(r.s_w == 0.0 for r in results.values())

        # Neither scorer nor judge should have been called
        assert len(scorer.call_log) == 0
        assert llm.call_count == 0

    # -----------------------------------------------------------------------
    # IT-7.8: Batch evaluation of 4 workspaces
    # -----------------------------------------------------------------------

    def test_batch_evaluation_four_workspaces(self, temp_dir, fake_issue):
        """Evaluate 4 workspaces in a single batch:
        - ws_perfect: S_exec=1.0 (skip LLM)
        - ws_partial: S_exec=0.5 (trigger LLM, S_llm=0.7)
        - ws_zero:    S_exec=0.0 (trigger LLM, S_llm=1.0)
        - ws_missing: no patch (S_w=0)

        Verify correct EvalResult for each workspace."""
        cache_dir = os.path.join(temp_dir, "criteria_cache")

        scores = {
            "ws_perfect": 1.0,
            "ws_partial": 0.5,
            "ws_zero": 0.0,
        }

        # LLM responses for ws_partial and ws_zero (ws_perfect skips LLM):
        # ws_partial: criteria + eval(0.7)
        # ws_zero:    criteria cached (same issue) + eval(1.0)
        responses = [
            _criteria_response(),          # criteria extraction (first non-perfect)
            _evaluation_response(0.7),     # eval for ws_partial
            _evaluation_response(1.0),     # eval for ws_zero (criteria cached)
        ]

        fake_scorer = FakeExecutionScorer(scores=scores)
        fake_llm = FakeLLMProvider(responses=responses)
        criteria_cache = CriteriaCache(cache_dir=cache_dir)
        llm_judge = LLMJudge(llm_provider=fake_llm, criteria_cache=criteria_cache)
        module = EvaluationModule(
            execution_scorer=fake_scorer,
            llm_judge=llm_judge,
            beta=0.3,
        )

        patches = {
            "ws_perfect": "patch content for ws_perfect",
            "ws_partial": "patch content for ws_partial",
            "ws_zero": "patch content for ws_zero",
            # ws_missing: intentionally absent
        }

        results = module.evaluate_all(patches)

        # ws_perfect: S_exec=1.0, skip LLM, S_w=1.0
        assert results["ws_perfect"].s_exec == 1.0
        assert results["ws_perfect"].s_llm is None
        assert results["ws_perfect"].s_w == pytest.approx(1.0)

        # ws_partial: S_exec=0.5, S_llm=0.7
        # S_w = 0.5 + (1 - 0.5) * 0.3 * 0.7 = 0.5 + 0.105 = 0.605
        assert results["ws_partial"].s_exec == 0.5
        assert results["ws_partial"].s_llm == pytest.approx(0.7)
        expected_partial = 0.5 + (1 - 0.5) * 0.3 * 0.7
        assert results["ws_partial"].s_w == pytest.approx(expected_partial, rel=1e-6)

        # ws_zero: S_exec=0.0, S_llm=1.0
        # S_w = 0.0 + (1 - 0.0) * 0.3 * 1.0 = 0.3
        assert results["ws_zero"].s_exec == 0.0
        expected_zero = 0.0 + (1 - 0.0) * 0.3 * 1.0
        assert results["ws_zero"].s_w == pytest.approx(expected_zero, rel=1e-6)

        # ws_missing: not in patches, should either be absent or have S_w=0
        if "ws_missing" in results:
            assert results["ws_missing"].s_w == pytest.approx(0.0, abs=1e-9)
