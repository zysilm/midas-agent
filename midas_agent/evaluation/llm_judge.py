"""LLM judge — criteria-based evaluation with self-reflection."""
from __future__ import annotations

import json

from midas_agent.evaluation.criteria_cache import CriteriaCache
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest
from midas_agent.types import Issue


class LLMJudge:
    """Evaluates a patch against LLM-extracted criteria for the issue.

    Two-step process:
    1. Extract (or retrieve cached) evaluation criteria for the issue.
    2. Ask the LLM to score the patch against those criteria.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        criteria_cache: CriteriaCache,
    ) -> None:
        self._llm_provider = llm_provider
        self._criteria_cache = criteria_cache

    def evaluate(self, patch: str, issue: Issue) -> float:
        """Return a quality score in [0.0, 1.0] for *patch* against *issue*.

        Step 1 -- retrieve or extract criteria via the cache.
        Step 2 -- call the LLM to score the patch against those criteria.
        """
        # Step 1: obtain criteria (cached or freshly extracted).
        criteria = self._criteria_cache.get_or_extract(
            issue.issue_id,
            self._extract_criteria,
        )

        # Step 2: evaluate the patch against the criteria.
        try:
            criteria_text = json.dumps(criteria)
        except (TypeError, ValueError):
            criteria_text = str(criteria)

        prompt = (
            "Evaluate the following patch against the criteria below. "
            "Return ONLY a single floating-point number between 0.0 and 1.0 "
            "representing the overall quality score.\n\n"
            f"Criteria:\n{criteria_text}\n\n"
            f"Patch:\n{patch}"
        )

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model="default",
            temperature=0.0,
        )
        response = self._llm_provider.complete(request)

        return self._parse_score(response.content)

    def _extract_criteria(self, issue_id: str) -> list[str]:
        """Ask the LLM to extract evaluation criteria for the given issue."""
        prompt = (
            "Extract a JSON list of evaluation criteria (strings) for "
            f"issue {issue_id}. Return ONLY a JSON array of strings."
        )
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model="default",
            temperature=0.0,
        )
        response = self._llm_provider.complete(request)

        try:
            criteria = json.loads(response.content)
            if isinstance(criteria, list):
                return criteria
        except (json.JSONDecodeError, TypeError):
            pass

        return ["correctness", "code quality"]

    @staticmethod
    def _parse_score(content: object) -> float:
        """Parse LLM response content into a clamped float score."""
        try:
            score = float(content)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return 0.5
        return max(0.0, min(1.0, score))
