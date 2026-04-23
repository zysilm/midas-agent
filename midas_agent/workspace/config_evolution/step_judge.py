"""Step completion judge — determines when a DAG step's goal is met.

Periodically evaluates the agent's trace against the step goal.
Returns the exact iteration where the step became complete.
"""
from __future__ import annotations

import logging
import re
from typing import Callable
from dataclasses import dataclass

from midas_agent.llm.types import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

STEP_JUDGE_PROMPT = """\
You are a step completion judge for a coding agent workflow.

The agent is working on a step with the following goal:

## Step goal
{goal}

## Agent trace (iterations so far)
{trace}

## Questions
1. Is the step goal completed? Answer DONE or NOT_DONE.
2. At which EXACT iteration number did the step become complete? \
(the iteration where the agent had gathered/done enough to satisfy the goal). \
If NOT_DONE, write 0.
3. Brief reason.

Answer format:
STATUS: DONE or NOT_DONE
COMPLETED_AT: <iteration number>
REASON: <one sentence>\
"""


@dataclass
class JudgeVerdict:
    done: bool
    completed_at: int
    reason: str


class StepJudge:
    """Evaluates whether a DAG step's goal has been met."""

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
        check_interval: int = 10,
    ) -> None:
        self._system_llm = system_llm
        self.check_interval = check_interval

    def should_check(self, iteration: int) -> bool:
        """Return True every check_interval iterations."""
        return iteration > 0 and iteration % self.check_interval == 0

    def evaluate(self, goal: str, trace: str) -> JudgeVerdict:
        """Ask the LLM whether the step goal is met.

        Args:
            goal: the step's completion criteria
            trace: formatted trace of iterations so far

        Returns:
            JudgeVerdict with done, completed_at, reason
        """
        prompt = STEP_JUDGE_PROMPT.format(goal=goal, trace=trace)

        try:
            resp = self._system_llm(
                LLMRequest(
                    messages=[{"role": "user", "content": prompt}],
                    model="default",
                )
            )
            return self._parse_response(resp.content or "")
        except Exception as e:
            logger.warning("StepJudge failed: %s", e)
            return JudgeVerdict(done=False, completed_at=0, reason=f"Judge error: {e}")

    @staticmethod
    def _parse_response(text: str) -> JudgeVerdict:
        """Parse STATUS, COMPLETED_AT, REASON from judge response."""
        done = False
        completed_at = 0
        reason = text

        # Parse STATUS
        status_match = re.search(r"STATUS:\s*(DONE|NOT_DONE|NOT DONE)", text, re.IGNORECASE)
        if status_match:
            status = status_match.group(1).upper().replace(" ", "_")
            done = status == "DONE"

        # Parse COMPLETED_AT
        at_match = re.search(r"COMPLETED_AT:\s*(\d+)", text)
        if at_match:
            completed_at = int(at_match.group(1))

        # Parse REASON
        reason_match = re.search(r"REASON:\s*(.+)", text, re.DOTALL)
        if reason_match:
            reason = reason_match.group(1).strip().split("\n")[0]

        return JudgeVerdict(done=done, completed_at=completed_at, reason=reason)

    @staticmethod
    def format_trace_for_judge(action_history: list, iterations: int) -> str:
        """Format action history into a compact trace for the judge.

        Each line: iter N: [TOOL] action_name(args...) or [TEXT] response
        """
        lines = []
        for i, record in enumerate(action_history):
            if i >= iterations:
                break
            args_str = ", ".join(
                f"{k}={repr(v)[:60]}" for k, v in record.arguments.items()
            ) if hasattr(record, "arguments") else ""
            result_short = (record.result or "")[:100].replace("\n", " ")
            lines.append(
                f"iter {i+1}: [TOOL] {record.action_name}({args_str}) → {result_short}"
            )
        return "\n".join(lines)
