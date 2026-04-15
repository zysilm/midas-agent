"""SWE-bench execution scorer — real Docker-based evaluation via swebench."""
from __future__ import annotations

import json
import logging

from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.types import Issue

logger = logging.getLogger(__name__)


class SWEBenchScorer(ExecutionScorer):
    """Evaluates patches using the official SWE-bench harness (Docker).

    Requires: Docker installed, ``poetry install -E swebench``.
    """

    def __init__(self, timeout: int = 1800, run_id: str = "midas") -> None:
        super().__init__(docker_image="", timeout=timeout)
        self._timeout = timeout
        self._run_id = run_id

    def score(self, patch: str, issue: Issue) -> float:
        """Apply patch in a SWE-bench Docker container and run tests.

        Returns S_exec: FAIL_TO_PASS pass rate. Returns 0.0 on regression
        (any PASS_TO_PASS test fails) or if patch fails to apply.
        """
        if not patch or not patch.strip():
            return 0.0

        try:
            import docker
            from swebench.harness.run_evaluation import run_instance
            from swebench.harness.test_spec.test_spec import make_test_spec
        except ImportError:
            logger.warning(
                "swebench or docker not installed, falling back to stub scorer"
            )
            return super().score(patch, issue)

        # Load the SWE-bench instance data for this issue.
        instance = self._load_instance(issue.issue_id)
        if instance is None:
            logger.error("Instance %s not found in SWE-bench", issue.issue_id)
            return 0.0

        prediction = {
            "instance_id": issue.issue_id,
            "model_patch": patch,
            "model_name_or_path": "midas-agent",
        }

        try:
            test_spec = make_test_spec(instance, namespace="swebench")
            client = docker.from_env()

            result = run_instance(
                test_spec=test_spec,
                pred=prediction,
                rm_image=False,
                force_rebuild=False,
                client=client,
                run_id=self._run_id,
                timeout=self._timeout,
            )

            if result is None:
                return 0.0

            instance_id, report = result
            return self._parse_report(report, issue)

        except Exception as e:
            logger.error("SWE-bench evaluation failed for %s: %s", issue.issue_id, e)
            return 0.0

    def _load_instance(self, instance_id: str) -> dict | None:
        """Load a single SWE-bench instance by ID."""
        try:
            from datasets import load_dataset

            ds = load_dataset(
                "princeton-nlp/SWE-bench_Verified",
                split="test",
            )
            for row in ds:
                if row["instance_id"] == instance_id:
                    return dict(row)
        except Exception as e:
            logger.error("Failed to load SWE-bench dataset: %s", e)
        return None

    def _parse_report(self, report: dict, issue: Issue) -> float:
        """Parse swebench report into S_exec score.

        Report structure (keyed by instance_id):
        {
            "instance_id": {
                "patch_is_None": bool,
                "patch_exists": bool,
                "patch_successfully_applied": bool,
                "resolved": bool,
                "tests_status": {...}  # if include_tests_status=True
            }
        }
        """
        # Report is keyed by instance_id.
        instance_report = report.get(issue.issue_id, report)

        # If it's still a nested dict, extract the inner report.
        if isinstance(instance_report, dict) and "resolved" not in instance_report:
            # Try first value
            for v in instance_report.values():
                if isinstance(v, dict):
                    instance_report = v
                    break

        if instance_report.get("resolved", False):
            return 1.0

        if not instance_report.get("patch_successfully_applied", False):
            return 0.0

        # Check tests_status for partial credit.
        tests_status = instance_report.get("tests_status", {})
        if not tests_status:
            return 0.0

        fail_to_pass = issue.fail_to_pass
        pass_to_pass = issue.pass_to_pass

        if not fail_to_pass:
            return 0.0

        # Any PASS_TO_PASS regression → 0.0
        for test in pass_to_pass:
            status = tests_status.get(test, "PASSED")
            if status == "FAILED":
                return 0.0

        # Count FAIL_TO_PASS that now pass.
        passed = sum(
            1 for test in fail_to_pass
            if tests_status.get(test, "FAILED") == "PASSED"
        )
        return passed / len(fail_to_pass)
