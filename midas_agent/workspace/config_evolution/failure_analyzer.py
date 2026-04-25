"""Failure analyzer — extracts abstract failure reasons from failed traces."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

FAILURE_ANALYSIS_PROMPT = """\
You are analyzing a failed coding agent trace. The agent attempted to fix \
a GitHub issue but the gold-standard test failed (score=0).

## Issue summary
{issue_summary}

## Agent trace (actions with full parameters, results truncated)
{trace}

## What the agent actually changed (str_replace edits)
{patch_summary}

## Gold test that failed
{gold_test_info}

## Gold test output (what the test actually checked)
{test_output}

## Task
1. Which DAG step went wrong? Choose from: {step_ids}
2. What SPECIFICALLY did the agent do wrong in that step?
3. What is the ABSTRACT lesson (no file/function names — must generalize \
to other issues)?

## Format
Respond in exactly this format:
STEP: <step_id>
MISTAKE: <what specifically went wrong>
LESSON: <one sentence abstract lesson for future runs>\
"""


def _build_rich_trace(raw_trace: str, max_result_chars: int = 200) -> str:
    """Keep full action names + params, truncate only tool results."""
    lines = []
    for line in raw_trace.split("\n"):
        if "\u2192" in line:
            parts = line.split("\u2192", 1)
            prefix = parts[0]
            result = parts[1].strip() if len(parts) > 1 else ""
            if len(result) > max_result_chars:
                result = result[:max_result_chars] + "..."
            lines.append(f"{prefix}\u2192 {result}")
        else:
            lines.append(line)
    return "\n".join(lines)


def _extract_patch_summary(raw_trace: str) -> str:
    """Extract str_replace edit actions from the trace."""
    edits = []
    for line in raw_trace.split("\n"):
        if "str_replace_editor" in line and "str_replace" in line and "old_str=" in line:
            edits.append(line)
    return "\n".join(edits) if edits else "(no edits found in trace)"


@dataclass
class FailureAnalysis:
    step_id: str
    mistake: str
    lesson: str


class FailureAnalyzer:
    """Extract abstract failure reasons from failed agent traces."""

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    def analyze(
        self,
        issue_summary: str,
        trace: str,
        step_ids: list[str],
        gold_test_names: list[str] | None = None,
        patch: str | None = None,
        test_output: str | None = None,
    ) -> FailureAnalysis | None:
        """Analyze a failed trace and return the step + mistake + lesson.

        Args:
            issue_summary: the issue description
            trace: full execution trace (from format_trace)
            step_ids: list of step IDs in the DAG config
            gold_test_names: FAIL_TO_PASS test names from SWE-bench
            patch: the agent's actual git diff (if available)
            test_output: SWE-bench test output showing what failed and why
        """
        rich_trace = _build_rich_trace(trace)
        patch_summary = _extract_patch_summary(trace)

        # Build gold test info
        if gold_test_names:
            gold_test_info = "Tests that must pass: " + ", ".join(gold_test_names)
        else:
            gold_test_info = "(gold test names not available)"

        if patch:
            gold_test_info += f"\n\nAgent's patch:\n{patch[:2000]}"

        # Gold test output — shows exactly what the test asserted and why it failed
        test_output_section = test_output[:3000] if test_output else "(test output not available)"

        prompt = FAILURE_ANALYSIS_PROMPT.format(
            issue_summary=issue_summary[:1000],
            trace=rich_trace,
            patch_summary=patch_summary,
            gold_test_info=gold_test_info,
            test_output=test_output_section,
            step_ids=", ".join(step_ids),
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

            # Response didn't parse — ask to retry with correct format
            logger.info("Failure analysis: response didn't parse (attempt %d/%d), retrying", attempt, max_retries)
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append({"role": "user", "content": (
                "Your response could not be parsed. Please respond in EXACTLY this format:\n"
                "STEP: <step_id>\n"
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
