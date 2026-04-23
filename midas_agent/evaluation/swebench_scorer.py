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

        instance_id, report = result
        return self._parse_report(report, issue)

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
