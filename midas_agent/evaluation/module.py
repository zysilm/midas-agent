"""Evaluation module facade."""
from __future__ import annotations

from dataclasses import dataclass

from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.evaluation.llm_judge import LLMJudge


@dataclass(frozen=True)
class EvalResult:
    workspace_id: str
    episode_id: str
    s_exec: float
    s_llm: float | None
    s_w: float


class EvaluationModule:
    def __init__(
        self,
        execution_scorer: ExecutionScorer,
        llm_judge: LLMJudge,
        beta: float = 0.3,
    ) -> None:
        raise NotImplementedError

    def evaluate_all(
        self,
        patches: dict[str, str],
    ) -> dict[str, EvalResult]:
        raise NotImplementedError

    def get_score(self, workspace_id: str, episode_id: str) -> float:
        raise NotImplementedError
