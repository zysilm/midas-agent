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
    def test_predicted_path_in_traceback_scores_one(self):
        run_bash = MagicMock(return_value=(
            'Traceback (most recent call last):\n'
            '  File "pkg/parser.py", line 12, in parse\n'
            'ValueError: empty'
        ))
        v = RCLVerifier()
        score = v.verify(
            Hypothesis(name="regex_or_parser_edge", predicted_path="pkg/parser.py",
                       test_payload="import pkg; pkg.parse('')"),
            _ctx(run_bash),
        )
        assert score == 1.0

    def test_traceback_without_predicted_path_scores_half(self):
        run_bash = MagicMock(return_value=(
            'Traceback (most recent call last):\n'
            '  File "pkg/other.py", line 3\nTypeError: x'
        ))
        v = RCLVerifier()
        score = v.verify(
            Hypothesis(name="inheritance_dispatch", predicted_path="pkg/parser.py",
                       test_payload="import pkg"),
            _ctx(run_bash),
        )
        assert score == 0.5

    def test_clean_run_scores_zero(self):
        run_bash = MagicMock(return_value="all good\n")
        v = RCLVerifier()
        score = v.verify(
            Hypothesis(name="state_mutation_order", predicted_path="pkg/parser.py",
                       test_payload="print('ok')"),
            _ctx(run_bash),
        )
        assert score == 0.0

    def test_empty_payload_scores_zero_and_skips_sandbox(self):
        run_bash = MagicMock()
        v = RCLVerifier()
        score = v.verify(Hypothesis(name="error_message_only"), _ctx(run_bash))
        assert score == 0.0
        run_bash.assert_not_called()

    def test_probe_file_is_cleaned_up(self):
        run_bash = MagicMock(return_value="Traceback (most recent call last):\nx")
        ctx = _ctx(run_bash)
        RCLVerifier().verify(
            Hypothesis(name="x", predicted_path="p", test_payload="print(1)"), ctx,
        )
        ctx.remove_file.assert_called_once()


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
