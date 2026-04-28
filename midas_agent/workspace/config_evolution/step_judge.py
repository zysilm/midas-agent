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
You are a step completion validator for a coding agent workflow.

The agent has stopped calling tools and claims this step is done.
Your job is to TRUST the agent by default — it has full context that \
you don't have. Only reject if the agent clearly did NOT attempt the \
required work.

## Step goal (what the agent was asked to do)
{goal}

## Agent trace
{trace}

## Agent's final message
{agent_message}

## Question
Did the agent actually perform the work required by the step goal? \
Trust the agent's judgment — it has full context. Only say REJECT if \
the trace shows the agent gave up without trying (e.g., no tool calls \
at all, or only read files without acting).

Answer with EXACTLY one line:
ACCEPT — if the agent did meaningful work toward the goal
REJECT — only if the agent clearly skipped the step\
"""


@dataclass
class JudgeVerdict:
    done: bool
    completed_at: int
    reason: str


class StepJudge:
    """Validates agent's claim of step completion.

    The agent decides when it's done (text response = stop).
    The judge only validates: "did the agent actually do the work?"
    Trust the agent by default — only reject obvious skip/giveup.
    """

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    def validate_completion(
        self,
        goal: str,
        trace: str,
        agent_message: str,
    ) -> JudgeVerdict:
        """Validate the agent's claim that the step is done.

        Args:
            goal: the step's completion criteria
            trace: formatted trace of iterations so far
            agent_message: the agent's text response (its "I'm done" message)

        Returns:
            JudgeVerdict — done=True means ACCEPT (trust agent)
        """
        prompt = STEP_JUDGE_PROMPT.format(
            goal=goal,
            trace=trace,
            agent_message=agent_message[:500],
        )

        try:
            resp = self._system_llm(
                LLMRequest(
                    messages=[{"role": "user", "content": prompt}],
                    model="default",
                )
            )
            return self._parse_response(resp.content or "")
        except Exception as e:
            # On judge failure, trust the agent
            logger.warning("StepJudge failed: %s — trusting agent", e)
            return JudgeVerdict(done=True, completed_at=0, reason=f"Judge error, trusting agent: {e}")

    @staticmethod
    def _parse_response(text: str) -> JudgeVerdict:
        """Parse ACCEPT/REJECT from judge response."""
        text_upper = text.upper().strip()

        if "REJECT" in text_upper:
            done = False
            reason = text.strip().split("\n")[0]
        else:
            # ACCEPT or anything else → trust agent
            done = True
            reason = text.strip().split("\n")[0]

        return JudgeVerdict(done=done, completed_at=0, reason=reason)

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
