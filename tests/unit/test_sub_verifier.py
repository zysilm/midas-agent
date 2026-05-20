"""Unit tests for HTA sub-verifiers.

Dependencies are mocked directly with unittest.mock — no fake/stub classes.
``run_bash`` is just a Callable, so a MagicMock stands in for the sandbox.
"""
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.stdlib.react_agent import ActionRecord
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.decision_point import Hypothesis
from midas_agent.workspace.hta.sub_verifier import (
    ContinuationVerifier,
    FixLocalityVerifier,
    RCLVerifier,
    SpecInterpretationVerifier,
    VerifierContext,
)


def _issue(**kw) -> Issue:
    return Issue(
        issue_id=kw.get("issue_id", "i1"),
        repo=kw.get("repo", "o/r"),
        description=kw.get("description", "the parser crashes on empty input"),
        fail_to_pass=kw.get("fail_to_pass", []),
    )


def _ctx(run_bash, issue=None) -> VerifierContext:
    return VerifierContext(
        issue=issue or _issue(),
        work_dir="/tmp/work",
        run_bash=run_bash,
        write_file=MagicMock(return_value="/tmp/work/_hta_probe.py"),
        remove_file=MagicMock(),
    )


@pytest.mark.unit
class TestRCLVerifier:
    """The Tier-0 RCL verifier greps the *issue text* (which the LLM did
    not author) for tokens from a static per-class lexicon (also outside
    LLM control). The verifier never touches the sandbox.
    """

    def test_two_tokens_in_issue_score_one(self):
        # operator_overload_path tokens include __array_ufunc__ and __eq__.
        issue = _issue(description=(
            'Comparing Row to None raises TypeError. Traceback:\n'
            '  __array_ufunc__ dispatched, then __eq__ raised.'
        ))
        score = RCLVerifier().verify(
            Hypothesis(name="operator_overload_path"),
            _ctx(MagicMock(), issue=issue),
        )
        assert score == 1.0

    def test_one_token_in_issue_scores_half(self):
        issue = _issue(description=(
            'A super() call in the subclass returns the wrong result.'
        ))
        score = RCLVerifier().verify(
            Hypothesis(name="inheritance_dispatch"),
            _ctx(MagicMock(), issue=issue),
        )
        assert score == 0.5

    def test_no_tokens_in_issue_scores_zero(self):
        issue = _issue(description='The widget rendered upside down.')
        score = RCLVerifier().verify(
            Hypothesis(name="serialization_roundtrip"),
            _ctx(MagicMock(), issue=issue),
        )
        assert score == 0.0

    def test_unknown_class_with_no_lexicon_scores_zero(self):
        # A class with no entry in RCL_EVIDENCE_TOKENS and no novel slug.
        issue = _issue(description='anything')
        score = RCLVerifier().verify(
            Hypothesis(name="not_a_real_class"),
            _ctx(MagicMock(), issue=issue),
        )
        assert score == 0.0

    def test_novel_class_uses_slug_derived_tokens(self):
        issue = _issue(description='cache invalidation race in the worker pool')
        score = RCLVerifier().verify(
            Hypothesis(name="__novel__:cache_invalidation_race"),
            _ctx(MagicMock(), issue=issue),
        )
        # Two slug tokens hit (cache, invalidation) -> 1.0.
        assert score == 1.0

    def test_does_not_touch_the_sandbox(self):
        """Anti-gaming property: the verifier must never run scripts or
        files in the sandbox — that path let the LLM author both
        question and answer (issue #44 B3)."""
        ctx = _ctx(MagicMock())
        RCLVerifier().verify(Hypothesis(name="operator_overload_path"), ctx)
        ctx.run_bash.assert_not_called()
        ctx.write_file.assert_not_called()
        ctx.remove_file.assert_not_called()

    def test_predicted_path_field_is_ignored(self):
        """Even a 'perfect' predicted_path is irrelevant — the verifier
        doesn't look at it. Confirms the LLM cannot author the oracle."""
        issue = _issue(description='The widget rendered upside down.')
        score = RCLVerifier().verify(
            Hypothesis(name="serialization_roundtrip",
                       predicted_path="The widget rendered upside down."),
            _ctx(MagicMock(), issue=issue),
        )
        assert score == 0.0


@pytest.mark.unit
class TestFixLocalityVerifier:
    def test_assertion_error_confirms_layer(self):
        run_bash = MagicMock(return_value="AssertionError: layer contract violated")
        score = FixLocalityVerifier().verify(
            Hypothesis(name="root_layer", test_payload="assert False"), _ctx(run_bash),
        )
        assert score == 1.0

    def test_unrelated_traceback_scores_half(self):
        run_bash = MagicMock(return_value="Traceback (most recent call last):\nTypeError")
        score = FixLocalityVerifier().verify(
            Hypothesis(name="surface_patch", test_payload="probe()"), _ctx(run_bash),
        )
        assert score == 0.5

    def test_empty_payload_scores_zero(self):
        score = FixLocalityVerifier().verify(
            Hypothesis(name="dual_fix"), _ctx(MagicMock()),
        )
        assert score == 0.0


@pytest.mark.unit
class TestSpecInterpretationVerifier:
    def test_overlap_with_issue_text_scores_higher(self):
        issue = _issue(description="serialization roundtrip loses timezone data")
        ctx = _ctx(MagicMock(), issue=issue)
        v = SpecInterpretationVerifier()
        on_topic = v.verify(
            Hypothesis(name="literal_reading", rationale="serialization roundtrip timezone"),
            ctx,
        )
        off_topic = v.verify(
            Hypothesis(name="inverse_reading", rationale="unrelated keyboard mouse widget"),
            ctx,
        )
        assert on_topic > off_topic

    def test_tier_is_two_when_system_llm_supplied(self):
        assert SpecInterpretationVerifier().tier == 0
        assert SpecInterpretationVerifier(system_llm=MagicMock()).tier == 2


@pytest.mark.unit
class TestContinuationVerifier:
    def _history(self, n, repeated=False):
        if repeated:
            return [ActionRecord("bash", {"command": "ls"}, "out", 0.0) for _ in range(n)]
        return [
            ActionRecord("bash", {"command": f"cmd{i}"}, "out", 0.0) for i in range(n)
        ]

    def test_abandon_favoured_on_long_repetitive_trace(self):
        ctx = _ctx(MagicMock())
        ctx.action_history = self._history(25, repeated=True)
        v = ContinuationVerifier()
        abandon = v.verify(Hypothesis(name="abandon"), ctx)
        persist = v.verify(Hypothesis(name="persist_same_path"), ctx)
        assert abandon > persist

    def test_persist_favoured_on_short_clean_trace(self):
        ctx = _ctx(MagicMock())
        ctx.action_history = self._history(3, repeated=False)
        v = ContinuationVerifier()
        persist = v.verify(Hypothesis(name="persist_same_path"), ctx)
        abandon = v.verify(Hypothesis(name="abandon"), ctx)
        assert persist > abandon

    def test_tier_is_one(self):
        assert ContinuationVerifier().tier == 1
