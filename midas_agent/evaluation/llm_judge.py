"""LLM judge — criteria-based evaluation with self-reflection."""
from midas_agent.evaluation.criteria_cache import CriteriaCache
from midas_agent.llm.provider import LLMProvider
from midas_agent.types import Issue


class LLMJudge:
    def __init__(
        self,
        llm_provider: LLMProvider,
        criteria_cache: CriteriaCache,
    ) -> None:
        raise NotImplementedError

    def evaluate(self, patch: str, issue: Issue) -> float:
        raise NotImplementedError
