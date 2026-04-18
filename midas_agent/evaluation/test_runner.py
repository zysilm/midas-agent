"""SWE-bench test runner — run fail_to_pass / pass_to_pass tests inside a workspace."""
from __future__ import annotations

from dataclasses import dataclass

# Sentinel prefix: when task_done returns text starting with this,
# the agent loop should NOT terminate — the agent needs to fix tests.
TEST_GATE_CONTINUE = "Tests failed"


@dataclass
class TestResult:
    """Result of running SWE-bench tests."""
    __test__ = False  # prevent pytest from collecting this dataclass

    all_passed: bool
    passed: int
    total: int
    failure_output: str


class SWEBenchTestRunner:
    """Runs SWE-bench tests inside a Docker container (or local bash).

    Parameters
    ----------
    bash_action : Action
        A BashAction or DockerBashAction whose execute(command=...) runs
        commands in the workspace environment.
    fail_to_pass : list[str]
        Test identifiers that should pass after the fix.
    pass_to_pass : list[str]
        Test identifiers that should continue to pass (regression guard).
    """

    def __init__(
        self,
        bash_action,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
    ) -> None:
        self._bash = bash_action
        self._fail_to_pass = fail_to_pass
        self._pass_to_pass = pass_to_pass

    def __call__(self) -> TestResult:
        all_tests = self._fail_to_pass + self._pass_to_pass
        if not all_tests:
            return TestResult(all_passed=True, passed=0, total=0, failure_output="")

        test_args = " ".join(all_tests)
        cmd = f"python -m pytest {test_args} --tb=short -q 2>&1"
        output = self._bash.execute(command=cmd)

        return self._parse_output(output, len(all_tests))

    @staticmethod
    def _parse_output(output: str, expected_total: int) -> TestResult:
        """Parse pytest output to extract pass/fail counts."""
        passed = 0
        failed = 0
        error = 0

        for line in output.splitlines():
            line = line.strip()
            # pytest summary line, e.g. "3 passed", "2 failed, 1 passed",
            # "1 error", "2 passed, 1 error"
            if " passed" in line or " failed" in line or " error" in line:
                import re
                m_passed = re.search(r"(\d+) passed", line)
                m_failed = re.search(r"(\d+) failed", line)
                m_error = re.search(r"(\d+) error", line)
                if m_passed:
                    passed = int(m_passed.group(1))
                if m_failed:
                    failed = int(m_failed.group(1))
                if m_error:
                    error = int(m_error.group(1))

        total = passed + failed + error
        if total == 0:
            # Could not parse — treat as failure
            total = expected_total
        all_passed = (failed == 0 and error == 0 and passed > 0)

        # Extract failure details (everything after FAILURES header or
        # short test summary, capped to avoid huge outputs)
        failure_output = ""
        if not all_passed:
            failure_output = _extract_failures(output)

        return TestResult(
            all_passed=all_passed,
            passed=passed,
            total=total,
            failure_output=failure_output,
        )


def _extract_failures(output: str, max_chars: int = 3000) -> str:
    """Extract the failure section from pytest output."""
    lines = output.splitlines()
    # Look for FAILURES or short test summary
    start = None
    for i, line in enumerate(lines):
        if "FAILURES" in line or "short test summary" in line:
            start = i
            break
    if start is not None:
        section = "\n".join(lines[start:])
    else:
        # No FAILURES header — return last portion of output
        section = output

    if len(section) > max_chars:
        section = section[:max_chars] + "\n... (truncated)"
    return section
