"""SWE-bench execution scorer — delegates to official run_instance.

Calls the official swebench ``run_instance()`` for evaluation. The patch
is applied and tests are run inside a Docker container managed by swebench.
The result report is parsed in-memory to produce an s_exec float.
"""
from __future__ import annotations

import logging
import uuid

from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.types import Issue

logger = logging.getLogger(__name__)

try:
    import docker
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.test_spec.test_spec import make_test_spec
    HAS_SWEBENCH = True
except ImportError:
    HAS_SWEBENCH = False


class SWEBenchScorer(ExecutionScorer):
    """Evaluates patches using SWE-bench's official run_instance."""

    def __init__(self, timeout: int = 1800) -> None:
        super().__init__(docker_image="", timeout=timeout)
        self._timeout = timeout
        self._run_id = f"midas-{uuid.uuid4().hex[:8]}"

    def score(self, patch: str, issue: Issue) -> float:
        """Score a patch. Also stores test_output for failure analysis."""
        self.last_test_output = ""

        if not patch or not patch.strip():
            return 0.0

        if not HAS_SWEBENCH:
            raise ImportError("swebench and docker packages required for evaluation")

        instance = self._load_instance(issue.issue_id)
        test_spec = make_test_spec(instance, namespace="swebench")
        client = docker.from_env()

        prediction = {
            "instance_id": issue.issue_id,
            "model_patch": patch,
            "model_name_or_path": "midas-agent",
        }

        result = run_instance(
            test_spec=test_spec,
            pred=prediction,
            rm_image=False,
            force_rebuild=True,
            client=client,
            run_id=self._run_id,
            timeout=self._timeout,
        )

        if result is None:
            logger.warning("SWE-bench evaluation returned None for %s", issue.issue_id)
            return 0.0

        # Capture test output from run_instance log
        self.last_test_output = self._read_test_log(issue.issue_id)

        instance_id, report = result
        return self._parse_report(report, issue)

    def _read_test_log(self, instance_id: str) -> str:
        """Read test output from SWE-bench evaluation.

        Prefers test_output.txt (has assertion details) over run_instance.log.
        Searches all run_ids (not just self._run_id) and picks the most recent.
        """
        import glob
        import os

        # Find test_output.txt across all run_ids, pick most recent
        pattern = f"logs/run_evaluation/*/midas-agent/{instance_id}/test_output.txt"
        matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        test_output_path = matches[0] if matches else None

        if test_output_path and os.path.isfile(test_output_path):
            try:
                with open(test_output_path) as f:
                    content = f.read()
                # Extract failure sections
                lines = content.split("\n")
                failure_lines = []
                capture = False
                for line in lines:
                    if any(k in line for k in ["FAILED", "AssertionError", "assert ", "Error", "raise "]):
                        capture = True
                    if capture:
                        failure_lines.append(line)
                        if len(failure_lines) > 80:
                            break
                    if capture and line.strip() == "" and len(failure_lines) > 3:
                        capture = False
                if failure_lines:
                    return "\n".join(failure_lines)
            except Exception:
                pass

        # Fallback to run_instance.log
        log_pattern = f"logs/run_evaluation/*/midas-agent/{instance_id}/run_instance.log"
        log_matches = sorted(glob.glob(log_pattern), key=os.path.getmtime, reverse=True)
        if log_matches:
            try:
                with open(log_matches[0]) as f:
                    return f.read()[-3000:]
            except Exception:
                pass

        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_instance(self, instance_id: str) -> dict | None:
        try:
            from datasets import load_dataset
            ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
            for row in ds:
                if row["instance_id"] == instance_id:
                    return dict(row)
        except Exception as e:
            logger.error("Failed to load SWE-bench dataset: %s", e)
        return None

    def _parse_report(self, report: dict, issue: Issue) -> float:
        instance_report = report.get(issue.issue_id, report)
        if isinstance(instance_report, dict) and "resolved" not in instance_report:
            for v in instance_report.values():
                if isinstance(v, dict):
                    instance_report = v
                    break

        if instance_report.get("resolved", False):
            return 1.0
        if not instance_report.get("patch_successfully_applied", False):
            return 0.0

        tests_status = instance_report.get("tests_status", {})
        if not tests_status:
            return 0.0

        fail_to_pass = issue.fail_to_pass
        pass_to_pass = issue.pass_to_pass
        if not fail_to_pass:
            return 0.0

        for test in pass_to_pass:
            status = tests_status.get(test, "PASSED")
            if status == "FAILED":
                return 0.0

        passed = sum(
            1 for test in fail_to_pass
            if tests_status.get(test, "FAILED") == "PASSED"
        )
        return passed / len(fail_to_pass)
