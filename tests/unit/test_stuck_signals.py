"""Unit tests for HTA-side stuck detection (issue #44 B6 / Q4)."""
import pytest

from llm_agent_toolkit.stdlib.react_agent import ActionRecord

from midas_agent.workspace.hta.execution_node import hta_stuck_signals


def _rec(name="bash", arguments=None, result=""):
    return ActionRecord(action_name=name, arguments=arguments or {}, result=result, timestamp=0.0)


@pytest.mark.unit
class TestStuckSignal1SameFileRead:
    def test_fires_on_five_reads_of_same_file(self):
        h = [_rec(arguments={"path": "/testbed/foo.py"}) for _ in range(5)]
        reason = hta_stuck_signals(h)
        assert reason is not None and reason.startswith("same_file_read_5x")
        assert "foo.py" in reason

    def test_does_not_fire_on_four_reads(self):
        h = [_rec(arguments={"path": "/testbed/foo.py"}) for _ in range(4)]
        assert hta_stuck_signals(h) is None

    def test_does_not_fire_across_different_files(self):
        h = [_rec(arguments={"path": f"/testbed/f{i}.py"}) for i in range(8)]
        assert hta_stuck_signals(h) is None

    def test_uses_file_argument_alias(self):
        # Some tools use "file" instead of "path".
        h = [_rec(arguments={"file": "bar.py"}) for _ in range(5)]
        reason = hta_stuck_signals(h)
        assert reason is not None and "bar.py" in reason


@pytest.mark.unit
class TestStuckSignal2SameErrorRepeated:
    def test_fires_on_three_consecutive_identical_errors(self):
        err = "ValueError: nothing matched here"
        h = [_rec(result=f"out\n{err}\n") for _ in range(3)]
        assert hta_stuck_signals(h) == "same_error_3x"

    def test_does_not_fire_on_two_consecutive_then_different(self):
        h = [
            _rec(result="ValueError: x"),
            _rec(result="ValueError: x"),
            _rec(result="ValueError: y"),
        ]
        assert hta_stuck_signals(h) is None

    def test_ignores_outputs_with_no_error_line(self):
        h = [_rec(result="all good"), _rec(result="all good"), _rec(result="all good")]
        assert hta_stuck_signals(h) is None


@pytest.mark.unit
class TestStuckSignal3BudgetWithoutEvidence:
    def test_fires_when_budget_burned_and_no_token_seen(self):
        h = [_rec(result="lorem ipsum dolor"), _rec(result="more output here")]
        reason = hta_stuck_signals(
            h, predicted_tokens=["__array_ufunc__"], budget_used_frac=0.7,
        )
        assert reason == "budget_60pct_no_evidence"

    def test_does_not_fire_when_token_seen(self):
        h = [_rec(result="hit on __array_ufunc__ in the traceback")]
        reason = hta_stuck_signals(
            h, predicted_tokens=["__array_ufunc__"], budget_used_frac=0.8,
        )
        assert reason is None

    def test_does_not_fire_below_threshold(self):
        h = [_rec(result="no evidence yet")]
        reason = hta_stuck_signals(
            h, predicted_tokens=["__array_ufunc__"], budget_used_frac=0.4,
        )
        assert reason is None

    def test_no_tokens_means_no_signal(self):
        h = [_rec(result="no evidence yet")]
        assert hta_stuck_signals(h, predicted_tokens=None, budget_used_frac=0.9) is None


@pytest.mark.unit
class TestStuckSignalsPrecedence:
    def test_signal_1_returns_first(self):
        # Both signal 1 (5x file read) and signal 2 (3x same error) would fire;
        # signal 1 is evaluated first.
        h = [_rec(arguments={"path": "p"}, result="ValueError: x") for _ in range(5)]
        assert hta_stuck_signals(h).startswith("same_file_read_5x")

    def test_no_history_with_no_tokens_returns_none(self):
        # No tokens to look for + nothing to grep -> nothing to detect.
        assert hta_stuck_signals([], predicted_tokens=None, budget_used_frac=0.9) is None

    def test_no_history_with_tokens_at_high_budget_fires_signal_3(self):
        # An agent that burned 90% of budget producing zero output, with
        # tokens we expected to see, is correctly flagged stuck.
        assert hta_stuck_signals([], predicted_tokens=["t"], budget_used_frac=0.9) == "budget_60pct_no_evidence"
