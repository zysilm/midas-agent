"""Unit tests for SWEBenchTestRunner."""
import pytest

from midas_agent.evaluation.test_runner import SWEBenchTestRunner, TestResult


class _MockBashAction:
    """Mock bash action that returns a canned output."""

    def __init__(self, output: str):
        self._output = output
        self.call_count = 0
        self.last_command = None

    def execute(self, **kwargs) -> str:
        self.call_count += 1
        self.last_command = kwargs.get("command", "")
        return self._output


@pytest.mark.unit
class TestSWEBenchTestRunner:
    """Tests for the SWE-bench test runner."""

    def test_all_tests_pass(self):
        """When pytest reports all passing, result is all_passed=True."""
        bash = _MockBashAction(
            "test_foo.py ...\n"
            "test_bar.py ..\n"
            "5 passed in 1.23s"
        )
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=["test_foo.py::test_a", "test_foo.py::test_b"],
            pass_to_pass=["test_bar.py::test_c"],
        )
        result = runner()
        assert result.all_passed is True
        assert result.passed == 5
        assert result.failure_output == ""

    def test_some_tests_fail(self):
        """When pytest reports failures, result is all_passed=False."""
        bash = _MockBashAction(
            "FAILED test_foo.py::test_a\n"
            "FAILED test_foo.py::test_b\n"
            "short test summary info\n"
            "2 failed, 3 passed in 2.50s"
        )
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=["test_foo.py::test_a", "test_foo.py::test_b"],
            pass_to_pass=["test_bar.py::test_c"],
        )
        result = runner()
        assert result.all_passed is False
        assert result.passed == 3
        assert result.total == 5

    def test_failure_output_captured(self):
        """Failure output contains test failure details."""
        output = (
            "test_foo.py F.\n"
            "= FAILURES =\n"
            "FAILED test_foo.py::test_a - AssertionError: expected 1, got 2\n"
            "1 failed, 1 passed in 0.5s"
        )
        bash = _MockBashAction(output)
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=["test_foo.py::test_a"],
            pass_to_pass=["test_foo.py::test_b"],
        )
        result = runner()
        assert result.all_passed is False
        assert "FAILED test_foo.py::test_a" in result.failure_output

    def test_timeout_treated_as_failure(self):
        """When bash returns a timeout message, result is all_passed=False."""
        bash = _MockBashAction("Command timed out after 120 seconds.")
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=["test_foo.py::test_a"],
            pass_to_pass=[],
        )
        result = runner()
        assert result.all_passed is False

    def test_empty_test_lists(self):
        """With no tests to run, result is all_passed=True."""
        bash = _MockBashAction("")
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=[],
            pass_to_pass=[],
        )
        result = runner()
        assert result.all_passed is True
        assert result.passed == 0
        assert result.total == 0

    def test_error_in_tests(self):
        """When pytest reports errors, result is all_passed=False."""
        bash = _MockBashAction(
            "ERROR test_foo.py::test_a\n"
            "1 error in 0.5s"
        )
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=["test_foo.py::test_a"],
            pass_to_pass=[],
        )
        result = runner()
        assert result.all_passed is False

    def test_passes_correct_command(self):
        """Runner passes the right pytest command to bash."""
        bash = _MockBashAction("2 passed in 0.1s")
        runner = SWEBenchTestRunner(
            bash_action=bash,
            fail_to_pass=["test_a.py::test_x"],
            pass_to_pass=["test_b.py::test_y"],
        )
        runner()
        assert "python -m pytest" in bash.last_command
        assert "test_a.py::test_x" in bash.last_command
        assert "test_b.py::test_y" in bash.last_command
        assert "--tb=line" in bash.last_command
