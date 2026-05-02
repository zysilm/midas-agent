"""Failure analyzer — extracts abstract failure reasons from failed patches."""
from __future__ import annotations

import json
import logging
import re
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
The agent's patch did NOT pass the gold test. Analyze the failure in three \
structured steps — complete each step before moving to the next:

### Step 1: Reconstruct the agent's strategy
What was the agent TRYING to do? Infer its implicit plan from the trajectory: \
what did it hypothesize as the root cause, what approach did it choose, and \
what was its intended fix? Do not judge yet — just reconstruct its reasoning.

### Step 2: Critique the strategy
Was the agent's strategy fundamentally sound, or was it flawed from the start? \
Did it misidentify the root cause? Did it edit the wrong layer of the system? \
Did it get stuck in a loop? Focus on WHERE the reasoning went wrong, not on \
what the correct answer is.

### Step 3: Extract the lesson
Based on steps 1-2, formulate a high-level lesson about what TYPE of strategic \
mistake the agent made. The lesson must:
- Be abstract (no file names, function names, or repo-specific details)
- Describe the FLAW IN REASONING, not the correct solution
- Generalize to other issues where an agent might make the same kind of mistake

After completing all three steps, call the submit_lesson tool.\
"""

# Tool definition for submit_lesson (OpenAI function calling format)
SUBMIT_LESSON_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_lesson",
        "description": (
            "Submit your failure analysis as a structured lesson. "
            "Call this exactly once after completing all three analysis steps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "step_id": {
                    "type": "string",
                    "description": "Which step failed (e.g. 'fix', 'localize', 'reproduce')",
                },
                "agent_intent": {
                    "type": "string",
                    "description": (
                        "What the agent was TRYING to do — its inferred strategy, "
                        "hypothesis about the root cause, and intended fix approach. "
                        "Reconstruct, do not judge."
                    ),
                },
                "strategy_flaw": {
                    "type": "string",
                    "description": (
                        "WHERE the agent's reasoning went wrong — did it misidentify "
                        "the root cause, edit the wrong layer, get stuck in a loop, etc. "
                        "Focus on the flaw in reasoning, not the correct answer."
                    ),
                },
                "lesson": {
                    "type": "string",
                    "description": (
                        "One-sentence abstract lesson about the TYPE of strategic mistake. "
                        "No file or function names — must generalize to other issues. "
                        "Describe the reasoning flaw, not the correct solution."
                    ),
                },
            },
            "required": ["step_id", "agent_intent", "strategy_flaw", "lesson"],
        },
    },
}


@dataclass
class FailureAnalysis:
    step_id: str
    agent_intent: str
    strategy_flaw: str
    lesson: str


class FailureAnalyzer:
    """Extract abstract failure reasons from failed agent patches.

    Uses LLM tool calling (submit_lesson) for structured extraction —
    LLMs are more reliable with tool calls than text format instructions.
    """

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

        Uses LLM tool calling for reliable structured extraction.
        ExpeL-style: trajectory + patch only, no gold test output.
        """
        if gold_test_names:
            gold_test_info = "Tests that must pass: " + ", ".join(gold_test_names)
        else:
            gold_test_info = "(gold test names not available)"

        patch_section = patch if patch else "(no patch produced)"
        trajectory_section = trajectory if trajectory else "(trajectory not available)"

        # Strip HTML comments from issue description (GitHub boilerplate noise)
        clean_summary = re.sub(r'<!--.*?-->', '', issue_summary, flags=re.DOTALL).strip()

        prompt = FAILURE_ANALYSIS_PROMPT.format(
            issue_summary=clean_summary[:1000],
            trajectory=trajectory_section,
            patch=patch_section,
            gold_test_info=gold_test_info,
        )

        messages = [
            {"role": "system", "content": "You are a failure analysis assistant. Analyze the failed attempt and submit a lesson using the submit_lesson tool."},
            {"role": "user", "content": prompt},
        ]
        tools = [SUBMIT_LESSON_TOOL]

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self._system_llm(
                    LLMRequest(messages=messages, model="default", tools=tools),
                )
            except Exception as e:
                logger.warning("Failure analysis API error (attempt %d/%d): %s", attempt, max_attempts, e)
                continue

            # Check if LLM called submit_lesson
            if resp.tool_calls:
                for tc in resp.tool_calls:
                    if tc.name == "submit_lesson":
                        args = tc.arguments
                        if isinstance(args, str):
                            args = json.loads(args)

                        step_id = args.get("step_id", "").strip().lower()
                        agent_intent = args.get("agent_intent", "").strip()
                        strategy_flaw = args.get("strategy_flaw", "").strip()
                        lesson = args.get("lesson", "").strip()

                        if not step_id or not lesson:
                            logger.info("Failure analysis: empty step_id or lesson (attempt %d/%d)", attempt, max_attempts)
                            break

                        # Match to closest valid step_id
                        if step_id not in step_ids:
                            for sid in step_ids:
                                if step_id in sid or sid in step_id:
                                    step_id = sid
                                    break
                            else:
                                step_id = step_ids[-1]

                        return FailureAnalysis(
                            step_id=step_id,
                            agent_intent=agent_intent,
                            strategy_flaw=strategy_flaw,
                            lesson=lesson,
                        )

            # LLM responded with text or wrong tool — retry
            logger.info(
                "Failure analysis: no submit_lesson call (attempt %d/%d), retrying",
                attempt, max_attempts,
            )
            # Append the bad response and a nudge to call the tool
            if resp.content:
                messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": (
                "You must call the submit_lesson tool to submit your analysis. "
                "Do not respond with text — use the tool."
            )})

        logger.warning("Failure analysis: exhausted %d attempts", max_attempts)
        return None
