"""Unit tests for ContinuationVerifier's stuck_reason dispatch (issue H1 D1).

The legacy adjacent-repetition tests live in tests/unit/test_sub_verifier.py
(TestContinuationVerifier). This file exercises the new behaviour: the
verifier reads ctx.stuck_reason and picks the appropriate continuation
strategy per the per-class baseline table.
"""
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.stdlib.react_agent import ActionRecord
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.decision_point import Hypothesis
from midas_agent.workspace.hta.sub_verifier import (
    ContinuationVerifier,
    VerifierContext,
)


def _issue() -> Issue:
    return Issue(
        issue_id="i1",
        repo="o/r",
        description="bug",
        fail_to_pass=[],
    )


def _ctx(stuck_reason: str | None = None, n_actions: int = 5) -> VerifierContext:
    return VerifierContext(
        issue=_issue(),
        work_dir="/tmp/work",
        run_bash=MagicMock(),
        write_file=MagicMock(),
        remove_file=MagicMock(),
        action_history=[
            ActionRecord("bash", {"command": f"cmd{i}"}, "out", 0.0)
            for i in range(n_actions)
        ],
        stuck_reason=stuck_reason,
    )


def _score_all(ctx: VerifierContext) -> dict[str, float]:
    v = ContinuationVerifier()
    return {
        name: v.verify(Hypothesis(name=name), ctx)
        for name in ("persist_same_path", "pivot_target", "pivot_evidence_type", "abandon")
    }


@pytest.mark.unit
class TestStuckReasonDispatch:
    def test_same_file_read_5x_picks_pivot_target(self):
        ctx = _ctx(stuck_reason="same_file_read_5x:/foo/bar.py", n_actions=8)
        scores = _score_all(ctx)
        winner = max(scores, key=scores.get)
        loser = min(scores, key=scores.get)
        assert winner == "pivot_target"
        assert loser == "persist_same_path"

    def test_budget_60pct_picks_abandon(self):
        ctx = _ctx(stuck_reason="budget_60pct_no_evidence", n_actions=12)
        scores = _score_all(ctx)
        winner = max(scores, key=scores.get)
        loser = min(scores, key=scores.get)
        assert winner == "abandon"
        assert loser == "persist_same_path"

    def test_same_error_picks_pivot_evidence(self):
        ctx = _ctx(stuck_reason="same_error_3x", n_actions=10)
        scores = _score_all(ctx)
        winner = max(scores, key=scores.get)
        assert winner == "pivot_evidence_type"

    def test_legacy_fallback_on_none_reason(self):
        # Short, clean (non-repetitive) trace + stuck_reason=None: the
        # legacy formula favours persist_same_path.
        ctx = _ctx(stuck_reason=None, n_actions=3)
        scores = _score_all(ctx)
        winner = max(scores, key=scores.get)
        assert winner == "persist_same_path"

    def test_legacy_fallback_on_unknown_reason(self):
        # An unknown stuck_reason must fall through to the legacy formula
        # without raising.
        ctx = _ctx(stuck_reason="some_future_signal", n_actions=3)
        scores = _score_all(ctx)
        # Legacy formula on a short clean trace favours persist.
        assert scores["persist_same_path"] >= scores["abandon"]

    def test_payload_suffix_stripped(self):
        # "same_file_read_5x:long/file/path.py" must be bucketed under
        # "same_file_read_5x", not silently fall through to legacy.
        ctx = _ctx(stuck_reason="same_file_read_5x:long/file/path.py", n_actions=5)
        scores = _score_all(ctx)
        # Bucketed correctly -> pivot_target wins, persist loses.
        assert max(scores, key=scores.get) == "pivot_target"

    def test_no_class_returns_neutral(self):
        ctx = _ctx(stuck_reason="same_file_read_5x:foo.py")
        v = ContinuationVerifier()
        novel = v.verify(Hypothesis(name="__novel__:something"), ctx)
        # Default baseline for unknown hyp.name in a known bucket: 0.30.
        assert novel == pytest.approx(0.30, abs=0.05)


@pytest.mark.unit
class TestLengthBonusBounded:
    def test_long_trace_amplifies_abandon(self):
        # Length bonus boosts abandon but does not overturn the baseline.
        short = _ctx(stuck_reason="same_file_read_5x:foo.py", n_actions=5)
        long_ = _ctx(stuck_reason="same_file_read_5x:foo.py", n_actions=30)
        v = ContinuationVerifier()
        abandon_short = v.verify(Hypothesis(name="abandon"), short)
        abandon_long = v.verify(Hypothesis(name="abandon"), long_)
        assert abandon_long > abandon_short
        # But pivot_target still wins on same_file_read_5x.
        pivot_long = v.verify(Hypothesis(name="pivot_target"), long_)
        assert pivot_long > abandon_long
