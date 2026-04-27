"""Failure analyzer — extracts abstract failure reasons from failed patches."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

FAILURE_ANALYSIS_PROMPT = """\
You are analyzing a failed coding agent attempt. The agent attempted to fix \
a GitHub issue but scored 0 (the fix was incorrect).

## Issue summary
{issue_summary}

## Agent's trajectory (actions and observations during execution)
{trajectory}

## Agent's final patch
{patch}

## Gold test that must pass
{gold_test_info}

## Task
The agent's patch did NOT pass the gold test. Based on the trajectory and patch:
1. What SPECIFICALLY did the agent do wrong?
2. What is the ABSTRACT lesson (no file/function names — must generalize \
to other issues)?

## Format
Respond in exactly this format:
STEP: fix
MISTAKE: <what specifically went wrong with the patch>
LESSON: <one sentence abstract lesson for future runs>\
"""


@dataclass
class FailureAnalysis:
    step_id: str
    mistake: str
    lesson: str


class FailureAnalyzer:
    """Extract abstract failure reasons from failed agent patches."""

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    def analyze(
        self,
        issue_summary: str,
        step_ids: list[str],
        gold_test_names: list[str] | None = None,
        patch: str | None = None,
        trajectory: str | None = None,
        **kwargs,
    ) -> FailureAnalysis | None:
        """Analyze a failed patch and return the mistake + lesson.

        Uses the agent's own trajectory and patch (ExpeL-style) — no gold
        test output to avoid leaking evaluation data into lessons.

        Args:
            issue_summary: the issue description
            step_ids: list of step IDs in the DAG config
            gold_test_names: FAIL_TO_PASS test names from SWE-bench
            patch: the agent's actual git diff
            trajectory: formatted trace of the agent's actions and observations
        """
        if gold_test_names:
            gold_test_info = "Tests that must pass: " + ", ".join(gold_test_names)
        else:
            gold_test_info = "(gold test names not available)"

        patch_section = patch if patch else "(no patch produced)"
        trajectory_section = trajectory if trajectory else "(trajectory not available)"

        # Strip HTML comments from issue description (GitHub boilerplate noise)
        import re
        clean_summary = re.sub(r'<!--.*?-->', '', issue_summary, flags=re.DOTALL).strip()

        prompt = FAILURE_ANALYSIS_PROMPT.format(
            issue_summary=clean_summary[:1000],
            trajectory=trajectory_section,
            patch=patch_section,
            gold_test_info=gold_test_info,
        )

        max_retries = 3
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(1, max_retries + 1):
            try:
                resp = self._system_llm(
                    LLMRequest(messages=messages, model="default"),
                )
            except Exception as e:
                logger.warning("Failure analysis API error (attempt %d/%d): %s", attempt, max_retries, e)
                continue

            result = self._parse_response(resp.content or "", step_ids)
            if result is not None:
                return result

            logger.info("Failure analysis: response didn't parse (attempt %d/%d), retrying", attempt, max_retries)
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append({"role": "user", "content": (
                "Your response could not be parsed. Please respond in EXACTLY this format:\n"
                "STEP: fix\n"
                "MISTAKE: <what went wrong>\n"
                "LESSON: <abstract lesson>"
            )})

        logger.warning("Failure analysis: exhausted %d retries", max_retries)
        return None

    @staticmethod
    def _parse_response(text: str, step_ids: list[str]) -> FailureAnalysis | None:
        step_id = ""
        mistake = ""
        lesson = ""

        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("STEP:"):
                step_id = line[5:].strip().lower()
            elif line.upper().startswith("MISTAKE:"):
                mistake = line[8:].strip()
            elif line.upper().startswith("LESSON:"):
                lesson = line[7:].strip()

        if not step_id or not lesson:
            return None

        # Match to closest valid step_id
        if step_id not in step_ids:
            for sid in step_ids:
                if step_id in sid or sid in step_id:
                    step_id = sid
                    break
            else:
                step_id = step_ids[-1]

        return FailureAnalysis(step_id=step_id, mistake=mistake, lesson=lesson)
