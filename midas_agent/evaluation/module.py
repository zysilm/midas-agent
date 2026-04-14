"""Evaluation module facade."""
from __future__ import annotations

from dataclasses import dataclass

from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.evaluation.llm_judge import LLMJudge
from midas_agent.types import Issue


@dataclass(frozen=True)
class EvalResult:
    workspace_id: str
    episode_id: str
    s_exec: float
    s_llm: float | None
    s_w: float


class EvaluationModule:
    """Facade that combines ExecutionScorer and LLMJudge into a single score.

    Composite formula:
        S_w = S_exec + (1 - S_exec) * beta * S_llm

    When S_exec == 1.0 the LLM judge is skipped (S_llm = None, S_w = 1.0).
    When beta == 0 the LLM judge is also skipped (S_w = S_exec).
    """

    def __init__(
        self,
        execution_scorer: ExecutionScorer,
        llm_judge: LLMJudge,
        beta: float = 0.3,
    ) -> None:
        self._execution_scorer = execution_scorer
        self._llm_judge = llm_judge
        self._beta = beta
        self._results: dict[str, EvalResult] = {}
        # Default issue used when none is supplied externally.
        self._issue = Issue(
            issue_id="default",
            repo="",
            description="",
        )

    def set_issue(self, issue: Issue) -> None:
        """Set the current issue for this evaluation episode."""
        self._issue = issue

    def evaluate_all(
        self,
        patches: dict[str, str],
    ) -> dict[str, EvalResult]:
        """Evaluate every workspace patch and return a dict of EvalResults."""
        results: dict[str, EvalResult] = {}

        for workspace_id, patch in patches.items():
            if not patch:
                # Empty patch -- score is zero, no scorers invoked.
                result = EvalResult(
                    workspace_id=workspace_id,
                    episode_id="current",
                    s_exec=0.0,
                    s_llm=None,
                    s_w=0.0,
                )
                results[workspace_id] = result
                self._results[workspace_id] = result
                continue

            # Use an Issue keyed by the default issue_id so that criteria
            # caching works across workspaces evaluating the same issue.
            issue = self._issue

            s_exec = self._execution_scorer.score(patch, issue)

            if s_exec == 1.0:
                # Perfect execution -- LLM judge is unnecessary.
                s_llm: float | None = None
                s_w = 1.0
            elif self._beta == 0:
                # Beta is zero -- LLM contribution is always zero.
                s_llm = None
                s_w = s_exec
            else:
                s_llm = self._llm_judge.evaluate(patch, issue)
                s_w = s_exec + (1 - s_exec) * self._beta * s_llm

            result = EvalResult(
                workspace_id=workspace_id,
                episode_id="current",
                s_exec=s_exec,
                s_llm=s_llm,
                s_w=s_w,
            )
            results[workspace_id] = result
            self._results[workspace_id] = result

        return results

    def get_score(self, workspace_id: str, episode_id: str) -> float:
        """Return the stored composite score for a workspace, or 0.0."""
        result = self._results.get(workspace_id)
        if result is None:
            return 0.0
        return result.s_w
