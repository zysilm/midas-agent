"""DecisionPointMetaJudge — decides whether the agent is at a decision point.

Two stages. First a pure-rule pre-filter: the three rule-driven decision points
(RCL / spec_interpretation / fix_locality_scope) are recognised without any LLM
call, and a state that is not stuck is rejected outright (the default
presumption is that a step is NOT a decision point). Only if the pre-filter is
inconclusive does the LLM judge run — in a clean, fresh context with the same
model — to decide investigation_continuation or to discover a novel
decision-point type.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse

from midas_agent.workspace.hta.decision_point import (
    DecisionPoint,
    DecisionPointRegistry,
    NOVEL_PREFIX,
    RuleTriggerInputs,
)
from midas_agent.workspace.hta.prompts import JUDGE_DECISION_POINT_TOOL, META_JUDGE_PROMPT

logger = logging.getLogger(__name__)


class DecisionPointMetaJudge:
    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
        registry: DecisionPointRegistry,
    ) -> None:
        self._system_llm = system_llm
        self._registry = registry

    def classify(
        self,
        rule_inputs: RuleTriggerInputs,
        is_stuck: bool,
        issue_summary: str = "",
        recent_trace: str = "",
    ) -> DecisionPoint | None:
        """Return the decision point for the current state, or None.

        The LLM judge is consulted only for a stuck state that no rule matched.
        """
        ruled = self._registry.rule_triggered(rule_inputs)
        if ruled is not None:
            return ruled

        # Default presumption: a non-stuck state is not a decision point.
        if not is_stuck:
            return None

        return self._llm_judge(issue_summary, recent_trace)

    def _llm_judge(self, issue_summary: str, recent_trace: str) -> DecisionPoint | None:
        prompt = META_JUDGE_PROMPT.format(
            issue_summary=issue_summary.strip()[:1500] or "(not available)",
            trace=recent_trace.strip()[:3000] or "(not available)",
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You judge whether a coding agent is at a decision point. "
                    "Always answer by calling the judge_decision_point tool."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self._system_llm(
                LLMRequest(
                    messages=messages, model="default", tools=[JUDGE_DECISION_POINT_TOOL],
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Meta-judge API error: %s", e)
            return None

        verdict = self._parse(resp)
        if verdict is None:
            return None

        # All three criteria must hold (HTA decision-point definition).
        if not (
            verdict.get("is_decision_point")
            and verdict.get("path_dependency")
            and verdict.get("enumerable_alternatives")
            and verdict.get("delayed_verification")
        ):
            return None

        decision_type = str(verdict.get("decision_type", "")).strip()
        if decision_type.startswith(NOVEL_PREFIX):
            # A newly identified decision-point type — synthesised on the fly.
            # The engine records it in TypedAdvantageMemory; until it registers
            # it is handled like investigation_continuation.
            logger.info("Meta-judge discovered novel decision type %r", decision_type)
            return DecisionPoint(
                decision_type=decision_type,
                seed_classes=["__novel__"],
                trigger_kind="meta_judge",
                verifier_tier=1,
            )
        return self._registry.get("investigation_continuation")

    @staticmethod
    def _parse(resp: LLMResponse) -> dict | None:
        if not resp.tool_calls:
            return None
        for tc in resp.tool_calls:
            if tc.name != "judge_decision_point":
                continue
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return None
            return args if isinstance(args, dict) else None
        return None
