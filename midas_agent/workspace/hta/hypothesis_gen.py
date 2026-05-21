"""HypothesisGenerator — produces G mutually exclusive hypotheses per decision point.

One LLM call (via the system LLM, with a function-calling tool) generates the
hypotheses. Generation is biased — but not bound — by SemanticExperienceMemory:
the relevant past experience for the decision type is injected into the prompt
as narrative guidance (issue H3), and when G has collapsed to 1 the prompt
asks for a single concrete commitment.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse

from midas_agent.workspace.hta.advantage_memory import SemanticExperienceMemory
from midas_agent.workspace.hta.decision_point import DecisionPoint, Hypothesis, NOVEL_PREFIX
from midas_agent.workspace.hta.prompts import (
    DECISION_TYPE_HELP,
    G1_GUIDANCE,
    GN_GUIDANCE,
    HYPOTHESIS_GEN_PROMPT,
    SUBMIT_HYPOTHESES_TOOL,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3


class HypothesisGenerator:
    def __init__(self, system_llm: Callable[[LLMRequest], LLMResponse]) -> None:
        self._system_llm = system_llm

    def generate(
        self,
        dp: DecisionPoint,
        issue_description: str,
        evidence: str,
        g: int,
        memory: SemanticExperienceMemory,
    ) -> list[Hypothesis]:
        """Generate up to ``g`` hypotheses for ``dp``. Returns [] only on total failure."""
        g = max(1, min(3, g))
        clean_issue = re.sub(r"<!--.*?-->", "", issue_description, flags=re.DOTALL).strip()

        prompt = HYPOTHESIS_GEN_PROMPT.format(
            decision_type=dp.decision_type,
            decision_type_help=DECISION_TYPE_HELP.get(dp.decision_type, ""),
            seed_classes="\n".join(f"- {c}" for c in dp.seed_classes),
            issue_description=clean_issue[:2000],
            evidence=evidence.strip()[:2000] or "(none yet)",
            bias_summary=memory.bias_summary(dp.decision_type),
            g=g,
            es_suffix="is" if g == 1 else "es",
            g_guidance=G1_GUIDANCE if g == 1 else GN_GUIDANCE,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You generate hypotheses for a coding agent's decision points. "
                    "Always answer by calling the submit_hypotheses tool."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        tools = [SUBMIT_HYPOTHESES_TOOL]

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._system_llm(
                    LLMRequest(messages=messages, model="default", tools=tools),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Hypothesis generation API error (attempt %d/%d): %s",
                    attempt, _MAX_ATTEMPTS, e,
                )
                continue

            hypotheses = self._parse(resp, dp, g)
            if hypotheses:
                return hypotheses

            if resp.content:
                messages.append({"role": "assistant", "content": resp.content})
            messages.append({
                "role": "user",
                "content": (
                    "You must call the submit_hypotheses tool. Do not respond with text."
                ),
            })

        logger.warning(
            "Hypothesis generation: exhausted %d attempts for %s",
            _MAX_ATTEMPTS, dp.decision_type,
        )
        return []

    def _parse(self, resp: LLMResponse, dp: DecisionPoint, g: int) -> list[Hypothesis]:
        if not resp.tool_calls:
            return []
        for tc in resp.tool_calls:
            if tc.name != "submit_hypotheses":
                continue
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return []
            raw = args.get("hypotheses") if isinstance(args, dict) else None
            if not raw:
                return []

            hypotheses: list[Hypothesis] = []
            for item in raw[:g]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("hypothesis_class", "")).strip()
                if not name:
                    continue
                name = self._normalize_class(name, dp)
                hypotheses.append(Hypothesis(
                    name=name,
                    rationale=str(item.get("rationale", "")).strip(),
                    predicted_path=str(item.get("predicted_path", "")).strip(),
                    test_payload=str(item.get("test_payload", "")).strip(),
                ))
            return hypotheses
        return []

    @staticmethod
    def _normalize_class(name: str, dp: DecisionPoint) -> str:
        """Keep a seed class as-is; tag anything else as a __novel__ slug."""
        if name in dp.seed_classes:
            return name
        if name.startswith(NOVEL_PREFIX):
            return name
        slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "unspecified"
        return f"{NOVEL_PREFIX}:{slug}"
