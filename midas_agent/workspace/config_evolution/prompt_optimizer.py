"""GEPA-based prompt optimizer for Configuration Evolution.

Replaces reflective mutation with DSPy GEPA (Guided Evolutionary Prompt
Adaptation).  GEPA provides brevity pressure and regression checking —
the two features missing from the old reflective self-rewrite.

Architecture:
  - StepPromptModule: wraps a step prompt as a DSPy-evolvable parameter
  - LLM-as-judge metric: system_llm scores alignment with successful traces
  - ConfigDatasetBuilder: sliding window of recent successful traces
  - GEPAConfigOptimizer: runs GEPA every N episodes on windowed data
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from types import SimpleNamespace
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.workspace.config_evolution.config_schema import (
    WorkflowConfig,
)
from midas_agent.workspace.config_evolution.mutator import validate_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DSPy import — graceful fallback when not installed
# ---------------------------------------------------------------------------
try:
    import dspy

    _BASE_MODULE = dspy.Module
    HAS_DSPY = True
except ImportError:
    HAS_DSPY = False

    class _StubModule:
        """Minimal stand-in so StepPromptModule can be defined without dspy."""
        def __init__(self) -> None:
            pass

    _BASE_MODULE = _StubModule  # type: ignore[misc,assignment]


# ===================================================================
# StepPromptModule
# ===================================================================

class StepPromptModule(_BASE_MODULE):  # type: ignore[misc]
    """Wraps a single DAG step prompt as a DSPy-evolvable parameter.

    GEPA mutates ``self.step_prompt`` while preserving the step's identity
    (id, tools, inputs).
    """

    def __init__(self, step_prompt: str, step_id: str) -> None:
        super().__init__()
        self.step_prompt = step_prompt
        self.step_id = step_id
        if HAS_DSPY:

            class StepTask(dspy.Signature):
                """Complete a coding workflow step following the prompt."""

                step_instructions: str = dspy.InputField(
                    desc="The step prompt/instructions to follow"
                )
                task_input: str = dspy.InputField(desc="The task to complete")
                output: str = dspy.OutputField(
                    desc="Your response following the step instructions"
                )

            self.predictor = dspy.ChainOfThought(StepTask)
        else:
            self.predictor = lambda **kwargs: None

    def forward(self, task_input: str):
        return self.predictor(
            step_instructions=self.step_prompt,
            task_input=task_input,
        )


# ===================================================================
# LLM-as-judge metric
# ===================================================================

JUDGE_PROMPT_TEMPLATE = """\
You are evaluating a coding agent's planned approach for a workflow step.

The agent was given step instructions and produced this plan:
<plan>
{output}
</plan>

A successful agent's actual execution trace for a similar task:
<trace>
{expected_trace}
</trace>

Evaluate on two criteria:
1. STRATEGY ALIGNMENT (0.0-1.0): Does the plan follow a similar strategy \
to the successful trace? (searching relevant files, reading code before \
editing, running tests, etc.)
2. CONCISENESS: Is the plan overly verbose or appropriately concise?

Respond in exactly this format:
SCORE: <float between 0.0 and 1.0>
FEEDBACK: <one paragraph explaining the score and what could improve>\
"""


def _parse_judge_response(text: str) -> tuple[float, str]:
    """Parse SCORE: and FEEDBACK: from judge LLM response."""
    score = 0.5  # default
    feedback = text or "No feedback."

    score_match = re.search(r"SCORE:\s*([\d.]+)", text or "")
    if score_match:
        try:
            score = max(0.0, min(1.0, float(score_match.group(1))))
        except ValueError:
            pass

    feedback_match = re.search(r"FEEDBACK:\s*(.+)", text or "", re.DOTALL)
    if feedback_match:
        feedback = feedback_match.group(1).strip()

    return score, feedback


def make_judge_metric(system_llm: Callable[[LLMRequest], LLMResponse]):
    """Create an LLM-as-judge metric closure for GEPA.

    Returns a metric function with the 5-argument GEPA signature that
    uses system_llm to score how well the module's output aligns with
    successful execution traces.
    """

    def judge_metric(example, prediction, trace=None, pred_name=None, pred_trace=None):
        output = prediction.output if hasattr(prediction, "output") else str(prediction)
        expected_trace = example.expected_behavior

        prompt = JUDGE_PROMPT_TEMPLATE.format(
            output=output[:1000],
            expected_trace=expected_trace[:1500],
        )

        if HAS_DSPY and dspy.settings.lm is not None:
            resp = dspy.settings.lm(prompt)
            if isinstance(resp, list) and resp:
                item = resp[0]
                raw = item.get("text", str(item)) if isinstance(item, dict) else str(item)
            else:
                raw = str(resp)
            score, feedback = _parse_judge_response(raw)
        else:
            resp = system_llm(
                LLMRequest(messages=[{"role": "user", "content": prompt}], model="default")
            )
            score, feedback = _parse_judge_response(resp.content or "")

        if HAS_DSPY:
            return dspy.Prediction(score=score, feedback=feedback)
        return {"score": score, "feedback": feedback}

    return judge_metric


# Legacy metric for tests that don't have a system_llm
def config_fitness_metric(example, prediction, trace=None, pred_name=None, pred_trace=None):
    """Simple word-overlap metric (fallback for tests without system_llm)."""
    output = prediction.output if hasattr(prediction, "output") else str(prediction)
    expected = example.expected_behavior

    brevity = max(0.0, 1.0 - len(output) / 2000)

    if not output.strip():
        score = 0.0
    else:
        expected_words = set(expected.lower().split())
        output_words = set(output.lower().split())
        if not expected_words:
            score = 0.5
        else:
            overlap = len(expected_words & output_words) / len(expected_words)
            score = 0.3 + 0.7 * overlap

    combined = 0.7 * score + 0.3 * brevity
    if HAS_DSPY:
        return dspy.Prediction(score=combined, feedback=f"Word overlap score: {score:.2f}, brevity: {brevity:.2f}")
    return {"score": combined, "feedback": f"Word overlap score: {score:.2f}, brevity: {brevity:.2f}"}


# ===================================================================
# Dataset builder (sliding window)
# ===================================================================

# Default sliding window size
DEFAULT_WINDOW_SIZE = 20


class ConfigDatasetBuilder:
    """Sliding window of recent successful traces for GEPA.

    Keeps the last ``max_window`` episodes.  Builds train / val / holdout
    splits (50/25/25) from the window for GEPA evaluation.

    Stores data as dicts internally.  ``build()`` returns ``dspy.Example``
    objects when DSPy is available, or ``SimpleNamespace`` as fallback.
    """

    def __init__(self, max_window: int = DEFAULT_WINDOW_SIZE) -> None:
        self._episodes: list[dict] = []
        self._max_window = max_window

    @property
    def size(self) -> int:
        return len(self._episodes)

    def add_episode(
        self,
        task_input: str,
        action_summary: str,
        score: float,
    ) -> None:
        """Record one successful episode's data.

        Args:
            task_input: the issue description
            action_summary: full execution trace (from format_trace)
            score: s_exec for this episode (should be >= 1.0)
        """
        self._episodes.append({
            "task_input": task_input,
            "expected_behavior": action_summary,
            "score": score,
        })
        # Sliding window: drop oldest if over limit
        if len(self._episodes) > self._max_window:
            self._episodes.pop(0)

    def _to_example(self, ep: dict):
        """Convert an episode dict to a dspy.Example or SimpleNamespace."""
        if HAS_DSPY:
            return dspy.Example(
                task_input=ep["task_input"],
                expected_behavior=ep["expected_behavior"],
                score=ep["score"],
            ).with_inputs("task_input")
        return SimpleNamespace(**ep)

    def build(self) -> tuple[list, list, list]:
        """Return ``(train, val, holdout)`` with a 50/25/25 split."""
        n = len(self._episodes)
        if n == 0:
            return [], [], []

        n_train = max(1, math.floor(n * 0.5))
        n_val = math.floor(n * 0.25)

        examples = [self._to_example(ep) for ep in self._episodes]
        train = examples[:n_train]
        val = examples[n_train : n_train + n_val]
        holdout = examples[n_train + n_val :]
        return train, val, holdout


# ===================================================================
# GEPA Config Optimizer
# ===================================================================

# Default: run GEPA every N episodes
DEFAULT_GEPA_INTERVAL = 5

# Minimum dataset size before GEPA is worth running
MIN_DATASET_SIZE = 5

# Maximum allowed prompt size after optimization
MAX_OPTIMIZED_PROMPT_CHARS = 2000

CONDENSE_PROMPT_TEMPLATE = """\
Condense this coding agent step prompt to under {max_chars} characters.
Keep the core strategy and key instructions. Remove redundant advice,
verbose explanations, and generic tips. Be direct and actionable.

Original prompt:
{prompt}

Respond with ONLY the condensed prompt, no explanation.\
"""


class GEPAConfigOptimizer:
    """Config optimizer using whole-config reflection on real execution traces.

    Replaces the old per-step DSPy GEPA with a reflection-based approach
    that sees both success and failure traces with real outcomes.

    Flow:
      1. Workspace calls ``record_episode()`` for successes
      2. Workspace calls ``record_failure()`` for failures (with reason)
      3. Workspace calls ``tick_episode()`` every episode
      4. Workspace calls ``maybe_optimize()`` after each episode
      5. If interval reached: ConfigReflector proposes improved config
      6. Returns new config or original if nothing changed
    """

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
        gepa_interval: int = DEFAULT_GEPA_INTERVAL,
        min_dataset_size: int = MIN_DATASET_SIZE,
        data_dir: str | None = None,
        window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> None:
        self._system_llm = system_llm
        self._gepa_interval = gepa_interval
        self._min_dataset_size = min_dataset_size
        self._dataset = ConfigDatasetBuilder(max_window=window_size)
        self._traces: list[dict] = []  # all traces (success + failure)
        self._episodes_since_last_optimization = 0
        self._data_dir = data_dir
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

    @property
    def dataset(self) -> ConfigDatasetBuilder:
        return self._dataset

    def load_dataset_from_dir(self, data_dir: str) -> None:
        """Reload dataset from persisted JSON files on disk."""
        if not os.path.isdir(data_dir):
            return
        for f in sorted(os.listdir(data_dir)):
            if not f.endswith(".json"):
                continue
            path = os.path.join(data_dir, f)
            with open(path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                continue
            score = data.get("score", 0.0)
            if score >= 1.0:
                self._dataset.add_episode(
                    data["issue_description"], data["trace"], score,
                )
            self._traces.append({
                "issue_summary": data.get("issue_description", "")[:200],
                "trace_summary": data.get("trace", "")[:500],
                "score": score,
                "failure_reason": data.get("failure_reason"),
            })
        if self._traces:
            logger.info("GEPA: reloaded %d traces (%d success) from %s",
                        len(self._traces), self._dataset.size, data_dir)

    def tick_episode(self) -> None:
        """Count an episode toward the GEPA trigger interval.

        Called every episode (success or failure).
        """
        self._episodes_since_last_optimization += 1

    def record_episode(
        self,
        task_input: str,
        action_summary: str,
        score: float,
        issue_id: str = "",
    ) -> None:
        """Record a successful episode for GEPA optimization."""
        self._dataset.add_episode(task_input, action_summary, score)
        self._traces.append({
            "issue_summary": task_input[:200],
            "trace_summary": action_summary[:500],
            "score": score,
            "failure_reason": None,
        })

        if self._data_dir:
            ep_num = self._dataset.size
            filename = f"ep{ep_num}_{issue_id}.json" if issue_id else f"ep{ep_num}.json"
            data = {
                "issue_id": issue_id,
                "issue_description": task_input,
                "trace": action_summary,
                "score": score,
                "failure_reason": None,
            }
            path = os.path.join(self._data_dir, filename)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("GEPA: saved success episode to %s", path)

    def record_failure(
        self,
        task_input: str,
        action_summary: str,
        score: float,
        failure_reason: str | None = None,
        issue_id: str = "",
    ) -> None:
        """Record a failed episode with optional failure reason."""
        self._traces.append({
            "issue_summary": task_input[:200],
            "trace_summary": action_summary[:500],
            "score": score,
            "failure_reason": failure_reason,
        })

        if self._data_dir:
            total = len(self._traces)
            filename = f"fail{total}_{issue_id}.json" if issue_id else f"fail{total}.json"
            data = {
                "issue_id": issue_id,
                "issue_description": task_input,
                "trace": action_summary,
                "score": score,
                "failure_reason": failure_reason,
            }
            path = os.path.join(self._data_dir, filename)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("GEPA: saved failure episode to %s", path)

    def should_optimize(self) -> bool:
        """Check whether it's time to run optimization."""
        return (
            self._episodes_since_last_optimization >= self._gepa_interval
            and len(self._traces) >= 1
        )

    # NOTE: maybe_optimize() and optimize() removed.
    # Config evolution now uses LessonStore (ExpeL-style retrieval)
    # instead of ConfigReflector-based prompt rewriting.
