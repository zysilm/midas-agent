"""SWE-bench execution scorer — Docker-based, fully in-memory evaluation.

Runs the full pipeline: apply patch → run eval script → parse test output
→ return report as an in-memory dict. No file-based caching — the report
is always computed from the current patch and test output string.

Artifacts (patch, test output, report) are written to a log directory for
post-hoc debugging but are never read back during scoring.
"""
from __future__ import annotations

import json
import logging
import traceback

from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.types import Issue

logger = logging.getLogger(__name__)


class SWEBenchScorer(ExecutionScorer):
    """Evaluates patches using SWE-bench Docker containers.

    Bypasses swebench's ``run_instance`` (which caches results to disk)
    and calls lower-level APIs directly.  Test output is parsed in-memory
    so the scorer is safe to call multiple times with different patches
    for the same issue.
    """

    def __init__(self, timeout: int = 1800, run_id: str = "midas") -> None:
        super().__init__(docker_image="", timeout=timeout)
        self._timeout = timeout
        self._run_id = run_id

    def score(self, patch: str, issue: Issue) -> float:
        if not patch or not patch.strip():
            return 0.0

        try:
            import docker
            from swebench.harness.test_spec.test_spec import make_test_spec
        except ImportError:
            logger.warning(
                "swebench or docker not installed, falling back to stub scorer"
            )
            return super().score(patch, issue)

        instance = self._load_instance(issue.issue_id)
        if instance is None:
            logger.error("Instance %s not found in SWE-bench", issue.issue_id)
            return 0.0

        try:
            test_spec = make_test_spec(instance, namespace="swebench")
            client = docker.from_env()
            report = self._run_and_grade(test_spec, patch, client)
            if report is None:
                return 0.0
            return self._parse_report(report, issue)
        except Exception as e:
            logger.error("SWE-bench evaluation failed for %s: %s", issue.issue_id, e)
            return 0.0

    # ------------------------------------------------------------------
    # Core evaluation: Docker container → test output string → report
    # ------------------------------------------------------------------

    def _run_and_grade(self, test_spec, patch: str, client) -> dict | None:
        """Run evaluation in Docker, grade test output in-memory."""
        from pathlib import PurePosixPath
        from swebench.harness.docker_build import build_container
        from swebench.harness.docker_utils import (
            cleanup_container,
            copy_to_container,
            exec_run_with_timeout,
        )
        from swebench.harness.constants import (
            DOCKER_PATCH,
            DOCKER_WORKDIR,
            DOCKER_USER,
            APPLY_PATCH_PASS,
            UTF8,
            RUN_EVALUATION_LOG_DIR,
        )
        from swebench.harness.run_evaluation import GIT_APPLY_CMDS

        instance_id = test_spec.instance_id

        # Log directory — write-only, for post-hoc debugging
        log_dir = (
            RUN_EVALUATION_LOG_DIR
            / self._run_id
            / "midas-agent"
            / instance_id
        )
        log_dir.mkdir(parents=True, exist_ok=True)

        container = None
        try:
            # Remove any stale container with the same name from a previous run
            container_name = f"sweb.eval.{instance_id}.{self._run_id}"
            try:
                stale = client.containers.get(container_name)
                stale.remove(force=True)
                logger.info("Removed stale container %s", container_name)
            except Exception:
                pass

            container = build_container(
                test_spec, client, self._run_id, logger, False, True,
            )
            container.start()
            logger.info("Container for %s started: %s", instance_id, container.id)

            # Write patch and copy to container
            patch_file = log_dir / "patch.diff"
            patch_file.write_text(patch)
            copy_to_container(container, patch_file, PurePosixPath(DOCKER_PATCH))

            # Apply patch
            applied = False
            for cmd in GIT_APPLY_CMDS:
                val = container.exec_run(
                    f"{cmd} {DOCKER_PATCH}",
                    workdir=DOCKER_WORKDIR,
                    user=DOCKER_USER,
                )
                if val.exit_code == 0:
                    logger.info("%s:\n%s", APPLY_PATCH_PASS, val.output.decode(UTF8))
                    applied = True
                    break
            if not applied:
                logger.warning("Patch failed to apply for %s", instance_id)
                return self._make_report(instance_id, applied=False)

            # Write and copy eval script
            eval_file = log_dir / "eval.sh"
            eval_file.write_text(test_spec.eval_script)
            copy_to_container(container, eval_file, PurePosixPath("/eval.sh"))

            # Run tests — get output as string
            test_output, timed_out, runtime = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", self._timeout,
            )
            logger.info("Test runtime for %s: %.2fs", instance_id, runtime)

            # Save artifacts (write-only)
            (log_dir / "test_output.txt").write_text(test_output)

            if timed_out:
                logger.warning("Tests timed out for %s", instance_id)
                return self._make_report(instance_id, applied=True)

            # Grade test output in-memory — no file read-back
            report = self._grade_output(test_spec, instance_id, test_output)

            # Save report artifact (write-only)
            (log_dir / "report.json").write_text(json.dumps(report, indent=4))

            resolved = report.get(instance_id, {}).get("resolved", False)
            logger.info("Result for %s: resolved=%s", instance_id, resolved)
            return report

        except Exception as e:
            logger.error(
                "Evaluation error for %s: %s\n%s",
                instance_id, e, traceback.format_exc(),
            )
            return None
        finally:
            if container is not None:
                cleanup_container(client, container, logger)

    # ------------------------------------------------------------------
    # In-memory grading — parses test output string, no file I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _grade_output(test_spec, instance_id: str, test_output: str) -> dict:
        """Parse raw test output into a SWE-bench report dict."""
        from swebench.harness.constants import (
            APPLY_PATCH_FAIL,
            RESET_FAILED,
            TESTS_ERROR,
            TESTS_TIMEOUT,
            START_TEST_OUTPUT,
            END_TEST_OUTPUT,
            FAIL_TO_PASS,
            PASS_TO_PASS,
            KEY_INSTANCE_ID,
            EvalType,
            ResolvedStatus,
            FAIL_ONLY_REPOS,
        )
        from swebench.harness.grading import (
            MAP_REPO_TO_PARSER,
            get_eval_tests_report,
            get_resolution_status,
        )

        report = {
            instance_id: {
                "patch_is_None": False,
                "patch_exists": True,
                "patch_successfully_applied": True,
                "resolved": False,
            }
        }

        # Check for bad status codes in output
        bad_codes = [
            code for code in [APPLY_PATCH_FAIL, RESET_FAILED, TESTS_ERROR, TESTS_TIMEOUT]
            if code in test_output
        ]
        if bad_codes:
            return report

        if START_TEST_OUTPUT not in test_output or END_TEST_OUTPUT not in test_output:
            return report

        # Extract test section and parse with repo-specific parser
        test_section = test_output.split(START_TEST_OUTPUT)[1].split(END_TEST_OUTPUT)[0]
        log_parser = MAP_REPO_TO_PARSER[test_spec.repo]
        eval_status_map = log_parser(test_section, test_spec)

        eval_ref = {
            KEY_INSTANCE_ID: test_spec.instance_id,
            FAIL_TO_PASS: test_spec.FAIL_TO_PASS,
            PASS_TO_PASS: test_spec.PASS_TO_PASS,
        }
        eval_type = (
            EvalType.FAIL_ONLY if test_spec.repo in FAIL_ONLY_REPOS
            else EvalType.PASS_AND_FAIL
        )

        tests_report = get_eval_tests_report(eval_status_map, eval_ref, eval_type=eval_type)
        if get_resolution_status(tests_report) == ResolvedStatus.FULL.value:
            report[instance_id]["resolved"] = True

        report[instance_id]["tests_status"] = tests_report
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_report(instance_id: str, applied: bool) -> dict:
        return {instance_id: {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": applied,
            "resolved": False,
        }}

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
