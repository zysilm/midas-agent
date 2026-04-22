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
    """Runs GEPA prompt optimization on DAG step prompts.

    Replaces the old ``ConfigMutator.reflective_self_rewrite()``.

    Flow:
      1. Workspace calls ``record_episode()`` after each successful episode
      2. Workspace calls ``maybe_optimize()`` after each episode
      3. If enough episodes accumulated (>= interval), GEPA runs on
         each step prompt using the sliding window dataset
      4. Constraint gating: size limit (with condensation) + holdout check
      5. Returns optimized config or original if gating rejects
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
        self._episodes_since_last_optimization = 0
        self._data_dir = data_dir
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

    @property
    def dataset(self) -> ConfigDatasetBuilder:
        return self._dataset

    def load_dataset_from_dir(self, data_dir: str) -> None:
        """Reload GEPA dataset from persisted JSON files on disk.

        Used when resuming training from a checkpoint.
        """
        if not os.path.isdir(data_dir):
            return
        for f in sorted(os.listdir(data_dir)):
            if not f.endswith(".json"):
                continue
            path = os.path.join(data_dir, f)
            with open(path) as fh:
                data = json.load(fh)
            self._dataset.add_episode(
                data["issue_description"], data["trace"], data["score"],
            )
        if self._dataset.size > 0:
            logger.info("GEPA: reloaded %d episodes from %s", self._dataset.size, data_dir)

    def record_episode(
        self,
        task_input: str,
        action_summary: str,
        score: float,
        issue_id: str = "",
    ) -> None:
        """Record a successful episode's data for future GEPA optimization.

        Only called for episodes with s_exec >= 1.0.  The action_summary
        should be the full execution trace (from format_trace), not a
        stats string.
        """
        self._dataset.add_episode(task_input, action_summary, score)
        self._episodes_since_last_optimization += 1

        # Persist to disk for cross-run reuse
        if self._data_dir:
            ep_num = self._dataset.size
            filename = f"ep{ep_num}_{issue_id}.json" if issue_id else f"ep{ep_num}.json"
            data = {
                "issue_id": issue_id,
                "issue_description": task_input,
                "trace": action_summary,
                "score": score,
            }
            path = os.path.join(self._data_dir, filename)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("GEPA: saved episode data to %s", path)

    def should_optimize(self) -> bool:
        """Check whether it's time to run GEPA."""
        return (
            self._episodes_since_last_optimization >= self._gepa_interval
            and self._dataset.size >= self._min_dataset_size
        )

    def maybe_optimize(self, config: WorkflowConfig) -> tuple[WorkflowConfig, bool]:
        """Run GEPA if conditions are met, otherwise return config as-is.

        Returns (config, changed) where changed indicates if GEPA
        produced a different config.
        """
        if not self.should_optimize():
            return config, False
        new_config = self.optimize(config)
        changed = new_config is not config
        return new_config, changed

    def optimize(self, config: WorkflowConfig) -> WorkflowConfig:
        """Run GEPA on each step prompt in the config.

        For each step:
          1. Wrap prompt as StepPromptModule
          2. Run GEPA with LLM-as-judge metric + trainset + valset
          3. Constraint gate: size check (with condensation) + holdout
          4. Accept or keep original

        Returns a new WorkflowConfig with optimized prompts.
        """
        if not HAS_DSPY:
            logger.warning("DSPy not installed — skipping GEPA optimization")
            return config

        train, val, holdout = self._dataset.build()
        if not train:
            logger.info("GEPA: no training data, skipping")
            return config

        logger.info(
            "GEPA optimization starting: %d train, %d val, %d holdout examples",
            len(train), len(val), len(holdout),
        )

        # Configure DSPy LM for GEPA's reflection calls
        system_lm = self._make_dspy_lm()

        # Create LLM-as-judge metric
        metric = make_judge_metric(self._system_llm)

        from midas_agent.workspace.config_evolution.config_schema import (
            ConfigMeta,
            StepConfig,
        )

        optimized_steps: list[StepConfig] = []
        any_changed = False

        for step in config.steps:
            new_prompt = self._optimize_step(
                step_id=step.id,
                step_prompt=step.prompt,
                train=train,
                val=val,
                holdout=holdout,
                system_lm=system_lm,
                metric=metric,
            )

            if new_prompt != step.prompt:
                any_changed = True
                logger.info(
                    "GEPA: step '%s' prompt updated (%d → %d chars)",
                    step.id, len(step.prompt), len(new_prompt),
                )

            optimized_steps.append(StepConfig(
                id=step.id,
                prompt=new_prompt,
                tools=list(step.tools),
                inputs=list(step.inputs),
            ))

        if any_changed:
            new_config = WorkflowConfig(
                meta=ConfigMeta(
                    name=config.meta.name,
                    description=config.meta.description,
                ),
                steps=optimized_steps,
            )
            # Final validation — reject if invalid
            errors = validate_config(new_config)
            if errors:
                logger.warning(
                    "GEPA: optimized config failed validation (%s), keeping original",
                    errors,
                )
                return config

            self._episodes_since_last_optimization = 0
            return new_config
        else:
            logger.info("GEPA: no step prompts changed, keeping original")
            self._episodes_since_last_optimization = 0
            return config

    def _optimize_step(
        self,
        step_id: str,
        step_prompt: str,
        train: list,
        val: list,
        holdout: list,
        system_lm,
        metric,
    ) -> str:
        """Optimize a single step prompt using GEPA.

        Returns the optimized prompt, or the original if gating rejects.
        """
        module = StepPromptModule(step_prompt=step_prompt, step_id=step_id)

        try:
            optimizer = dspy.GEPA(
                metric=metric,
                reflection_lm=system_lm,
                max_metric_calls=60,
                candidate_selection_strategy="pareto",
                num_threads=16,
            )
            optimized_module = optimizer.compile(
                module,
                trainset=train,
                valset=val if val else None,
            )
        except Exception as e:
            logger.warning("GEPA failed for step '%s': %s", step_id, e)
            return step_prompt

        # Extract the optimized prompt
        new_prompt = getattr(optimized_module, "step_prompt", step_prompt)

        # --- Constraint gating ---

        # 1. Size check — condense if too large
        if len(new_prompt) > MAX_OPTIMIZED_PROMPT_CHARS:
            logger.info(
                "GEPA: step '%s' prompt too large (%d chars), condensing...",
                step_id, len(new_prompt),
            )
            new_prompt = self._condense_prompt(new_prompt, MAX_OPTIMIZED_PROMPT_CHARS)
            if len(new_prompt) > MAX_OPTIMIZED_PROMPT_CHARS:
                logger.info(
                    "GEPA: step '%s' rejected — condensation failed (%d chars)",
                    step_id, len(new_prompt),
                )
                return step_prompt

        # 2. Non-empty check
        if not new_prompt.strip():
            logger.info("GEPA: step '%s' rejected — empty prompt", step_id)
            return step_prompt

        # 3. Holdout regression check
        if holdout and not self._passes_holdout_check(
            module, optimized_module, holdout, metric
        ):
            logger.info(
                "GEPA: step '%s' rejected — holdout regression", step_id
            )
            return step_prompt

        return new_prompt

    def _condense_prompt(self, prompt: str, max_chars: int) -> str:
        """Ask system_llm to condense a prompt that exceeds the size limit."""
        condense_prompt = CONDENSE_PROMPT_TEMPLATE.format(
            max_chars=max_chars,
            prompt=prompt,
        )
        try:
            resp = self._system_llm(
                LLMRequest(
                    messages=[{"role": "user", "content": condense_prompt}],
                    model="default",
                )
            )
            condensed = (resp.content or "").strip()
            if condensed:
                logger.info(
                    "GEPA: condensed prompt from %d to %d chars",
                    len(prompt), len(condensed),
                )
                return condensed
        except Exception as e:
            logger.warning("GEPA: condensation failed: %s", e)
        return prompt  # fallback to original

    def _passes_holdout_check(
        self,
        original_module: StepPromptModule,
        optimized_module: StepPromptModule,
        holdout: list,
        metric,
    ) -> bool:
        """Check that the optimized module doesn't regress on the holdout set.

        Returns True if the optimized module scores >= original on average.
        """
        def _avg_score(mod: StepPromptModule, data: list) -> float:
            total = 0.0
            count = 0
            for example in data:
                try:
                    pred = mod.forward(task_input=example.task_input)
                    result = metric(example, pred)
                    # Extract score from dspy.Prediction or dict
                    if hasattr(result, "score"):
                        total += result.score
                    elif isinstance(result, dict):
                        total += result.get("score", 0.0)
                    count += 1
                except Exception:
                    continue
            return total / count if count > 0 else 0.0

        original_score = _avg_score(original_module, holdout)
        optimized_score = _avg_score(optimized_module, holdout)

        logger.info(
            "Holdout check: original=%.3f, optimized=%.3f",
            original_score, optimized_score,
        )
        return optimized_score >= original_score

    def _make_dspy_lm(self):
        """Create a DSPy LM for GEPA's reflection calls.

        Reads model/api_key from the same sources as the rest of Midas
        (env vars → .midas/config.yaml).
        """
        try:
            from midas_agent.resolver import resolve_llm_config
            llm_config = resolve_llm_config()
            kwargs = {"model": llm_config.model}
            if llm_config.api_key:
                kwargs["api_key"] = llm_config.api_key
            if llm_config.api_base:
                kwargs["api_base"] = llm_config.api_base
            lm = dspy.LM(**kwargs)
            dspy.configure(lm=lm)
            return lm
        except Exception as e:
            logger.warning("Could not create DSPy LM: %s", e)
            return None
