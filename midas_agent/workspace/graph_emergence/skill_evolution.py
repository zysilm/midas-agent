"""Skill evolution components: SkillModule, fitness metric, dataset builder,
and initial skill creation from action history."""
from __future__ import annotations

import json
import math
from typing import Callable
from types import SimpleNamespace

from midas_agent.llm.types import LLMRequest
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.skill import Skill

# ---------------------------------------------------------------------------
# DSPy import -- graceful fallback when the package is not installed
# ---------------------------------------------------------------------------
try:
    import dspy

    _BASE_MODULE = dspy.Module
    HAS_DSPY = True
except ImportError:
    HAS_DSPY = False

    class _StubModule:
        """Minimal stand-in so SkillModule can be defined without dspy."""

        def __init__(self) -> None:
            pass

    _BASE_MODULE = _StubModule  # type: ignore[misc,assignment]


# ===================================================================
# SkillModule (DSPy wrapper)
# ===================================================================


class SkillModule(_BASE_MODULE):  # type: ignore[misc]
    """Wraps a skill's content as a DSPy-evolvable parameter."""

    def __init__(self, skill_text: str) -> None:
        super().__init__()
        self.skill_text = skill_text
        if HAS_DSPY:

            class TaskWithSkill(dspy.Signature):
                """Complete a task following the provided skill instructions."""

                skill_instructions: str = dspy.InputField(
                    desc="The skill instructions to follow"
                )
                task_input: str = dspy.InputField(desc="The task to complete")
                output: str = dspy.OutputField(
                    desc="Your response following the skill instructions"
                )

            self.predictor = dspy.ChainOfThought(TaskWithSkill)
        else:
            # Provide a callable placeholder that tests can mock via
            # ``patch.object(module, "predictor")``.
            self.predictor = lambda **kwargs: None

    def forward(self, task_input: str):
        return self.predictor(
            skill_instructions=self.skill_text,
            task_input=task_input,
        )


# ===================================================================
# Fitness metric
# ===================================================================


def skill_fitness_metric(example, prediction, trace=None) -> dict:
    """Multi-objective fitness scoring: accuracy (word overlap) + brevity.

    Returns ``{"scores": {"accuracy": float, "brevity": float}}``.
    """
    output: str = prediction.output
    expected: str = example.expected_behavior

    # --- brevity ---
    brevity = max(0.0, 1.0 - len(output) / 5000)

    # --- accuracy ---
    if not output.strip():
        return {"scores": {"accuracy": 0.0, "brevity": brevity}}

    expected_words = set(expected.lower().split())
    output_words = set(output.lower().split())

    if not expected_words:
        accuracy = 0.5
    else:
        overlap = len(expected_words & output_words) / len(expected_words)
        accuracy = 0.3 + 0.7 * overlap

    return {"scores": {"accuracy": accuracy, "brevity": brevity}}


# ===================================================================
# Initial skill creation (Path A)
# ===================================================================


def create_initial_skill(
    system_llm: Callable,
    action_history: list[ActionRecord],
    eval_results: dict,
) -> Skill | None:
    """Create the first skill from an agent's action history.

    Returns ``None`` when there is nothing to learn from (empty history).
    """
    if not action_history:
        return None

    # Build a concise summary of the actions taken
    action_lines: list[str] = []
    for rec in action_history:
        args_str = json.dumps(rec.arguments) if rec.arguments else ""
        action_lines.append(
            f"- {rec.action_name}({args_str}) -> {rec.result[:200]}"
        )
    actions_block = "\n".join(action_lines)

    prompt_text = (
        "You are a skill extraction engine. Based on the following action "
        "history from an agent that solved a coding task, create a reusable "
        "skill document.\n\n"
        "The skill should capture PATTERNS, not specific fixes:\n"
        "- What type of problem was this? (error message, computation, API)\n"
        "- What investigation steps worked?\n"
        "- What pitfalls were encountered?\n"
        "- How was the fix verified?\n\n"
        "Keep it focused but generalizable to similar problems.\n"
        "Do NOT mention specific line numbers or variable values.\n\n"
        f"## Action History\n{actions_block}\n\n"
        f"## Evaluation Results\n{json.dumps(eval_results)}\n\n"
        "Respond with ONLY a JSON object with keys: name, description, content.\n"
        "- name: short, descriptive (e.g. 'debug_computation_errors')\n"
        "- description: one sentence for marketplace matching\n"
        "- content: reusable procedure with pitfalls (max 5000 chars)"
    )

    request = LLMRequest(
        messages=[
            {"role": "system", "content": "You extract reusable skills from agent traces."},
            {"role": "user", "content": prompt_text},
        ],
        model="default",
    )

    try:
        response = system_llm(request)
        data = json.loads(response.content)

        content = data["content"]
        if len(content) > 5000:
            content = content[:5000]

        return Skill(
            name=data["name"],
            description=data["description"],
            content=content,
        )
    except Exception:
        return None


# ===================================================================
# Dataset builder
# ===================================================================


class SkillDatasetBuilder:
    """Builds train / val / holdout splits from real training episodes."""

    def __init__(self) -> None:
        self._episodes: list[SimpleNamespace] = []

    def add_episode(
        self, task_input: str, action_summary: str, score: float
    ) -> None:
        self._episodes.append(
            SimpleNamespace(
                task_input=task_input,
                expected_behavior=action_summary,
                score=score,
            )
        )

    def build(self) -> tuple[list, list, list]:
        """Return ``(train, val, holdout)`` with a 50/25/25 split."""
        n = len(self._episodes)
        if n == 0:
            return [], [], []

        n_train = max(1, math.floor(n * 0.5))
        n_val = math.floor(n * 0.25)
        n_holdout = n - n_train - n_val

        train = self._episodes[:n_train]
        val = self._episodes[n_train : n_train + n_val]
        holdout = self._episodes[n_train + n_val :]
        return train, val, holdout
