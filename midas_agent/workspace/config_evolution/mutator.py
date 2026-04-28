"""Config mutation utilities — validation and structural constraint gating.

The ConfigMutator class (reflective self-rewrite) has been replaced by
GEPAConfigOptimizer in prompt_optimizer.py.  This module retains:

  - validate_config(): deterministic YAML/DAG validation
  - _validate_mutation(): structural constraint gate (same IDs, tools, inputs)
  - _config_to_yaml(): serialise WorkflowConfig to YAML
"""
from __future__ import annotations

import logging
from collections import deque

import yaml

from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)

logger = logging.getLogger(__name__)

# Valid action names that can appear in step.tools
VALID_TOOLS = {"bash", "str_replace_editor", "task_done"}

# Prompt size limit per step
MAX_STEP_PROMPT_CHARS = 2000

# Maximum allowed growth ratio for a single prompt mutation (30%)
MAX_PROMPT_GROWTH_RATIO = 0.3


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _config_to_yaml(config: WorkflowConfig) -> str:
    """Serialise a WorkflowConfig to readable YAML."""
    data = {
        "meta": {
            "name": config.meta.name,
            "description": config.meta.description,
        },
        "steps": [
            {
                "id": s.id,
                "prompt": s.prompt,
                "tools": s.tools,
                "inputs": s.inputs,
                **({"goal": s.goal} if s.goal else {}),
            }
            for s in config.steps
        ],
    }
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def validate_config(config: WorkflowConfig, *, skip_prompt_length: bool = False) -> list[str]:
    """Deterministic validation of a WorkflowConfig.

    Returns a list of error strings.  Empty list = valid.
    Checks: YAML structure, step IDs unique, tools legal, inputs
    reference existing steps, DAG is acyclic, prompts non-empty.

    Args:
        skip_prompt_length: if True, skip the per-step prompt size check.
            Used for merged configs where issue text inflates prompts.
    """
    errors: list[str] = []

    if not config.steps:
        errors.append("Config must have at least one step.")
        return errors

    step_ids = [s.id for s in config.steps]

    # Unique IDs
    seen: set[str] = set()
    for sid in step_ids:
        if sid in seen:
            errors.append(f"Duplicate step id: '{sid}'.")
        seen.add(sid)

    for step in config.steps:
        # Valid tools
        for tool in step.tools:
            if tool not in VALID_TOOLS:
                errors.append(
                    f"Step '{step.id}': unknown tool '{tool}'. "
                    f"Valid tools: {sorted(VALID_TOOLS)}."
                )

        # Inputs reference existing steps
        for inp in step.inputs:
            if inp not in seen:
                errors.append(
                    f"Step '{step.id}': input '{inp}' does not match any step id."
                )

        # Non-empty prompt
        if not step.prompt.strip():
            errors.append(f"Step '{step.id}': prompt is empty.")

        # Prompt size
        if not skip_prompt_length and len(step.prompt) > MAX_STEP_PROMPT_CHARS:
            errors.append(
                f"Step '{step.id}': prompt is {len(step.prompt)} chars "
                f"(max {MAX_STEP_PROMPT_CHARS})."
            )

    # Acyclic check (Kahn's algorithm)
    in_degree = {s.id: 0 for s in config.steps}
    dependents: dict[str, list[str]] = {s.id: [] for s in config.steps}
    for step in config.steps:
        for inp in step.inputs:
            if inp in in_degree:
                in_degree[step.id] += 1
                dependents[inp].append(step.id)

    queue: deque[str] = deque(sid for sid, d in in_degree.items() if d == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for dep in dependents[current]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    if visited != len(config.steps):
        cycle_ids = [sid for sid, d in in_degree.items() if d > 0]
        errors.append(f"Cyclic dependency among steps: {cycle_ids}.")

    # At least one entry node
    if all(s.inputs for s in config.steps):
        errors.append("No entry node (at least one step must have inputs=[]).")

    return errors


# ------------------------------------------------------------------
# Structural constraint gate for mutations
# ------------------------------------------------------------------

def _validate_mutation(
    original: WorkflowConfig,
    mutated: WorkflowConfig,
) -> bool:
    """Check that a mutation preserves DAG structure and satisfies constraints.

    A valid mutation may ONLY change the ``prompt`` field of each step.
    Step IDs, tools, inputs, and step count must remain identical.

    Additional constraints:
      - Prompts must be non-empty
      - Prompts must not exceed MAX_STEP_PROMPT_CHARS
      - Individual prompt growth must not exceed MAX_PROMPT_GROWTH_RATIO (30%)

    Returns True if the mutation is valid, False otherwise.
    """
    # Same number of steps
    if len(original.steps) != len(mutated.steps):
        return False

    for old_step, new_step in zip(original.steps, mutated.steps):
        # Same step ID
        if old_step.id != new_step.id:
            return False

        # Same tools
        if old_step.tools != new_step.tools:
            return False

        # Same inputs (DAG structure preserved)
        if old_step.inputs != new_step.inputs:
            return False

        # Non-empty prompt
        if not new_step.prompt.strip():
            return False

        # Prompt size limit
        if len(new_step.prompt) > MAX_STEP_PROMPT_CHARS:
            return False

        # Growth cap: prevent inflation of already-long prompts.
        # Only enforced when original prompt is ≥ 100 chars — short
        # prompts naturally have high percentage growth.
        old_len = len(old_step.prompt)
        new_len = len(new_step.prompt)
        if old_len >= 100:
            growth = (new_len - old_len) / old_len
            if growth > MAX_PROMPT_GROWTH_RATIO:
                return False

    return True
