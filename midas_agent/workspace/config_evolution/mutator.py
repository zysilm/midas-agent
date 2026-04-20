"""Config mutator — reproduction and reflective self-rewrite via SystemLLM.

The mutation loop: LLM generates YAML → deterministic validator checks it →
if invalid, feed errors back to LLM → retry until valid.  All calls go
through SystemLLM (unmetered, system cost).
"""
from __future__ import annotations

import json
import logging
from collections import deque
from typing import Callable

import yaml

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)

logger = logging.getLogger(__name__)

# Retry budget for the validate-fix loop
MAX_VALIDATION_RETRIES = 3

# Valid action names that can appear in step.tools
VALID_TOOLS = {"bash", "str_replace_editor", "task_done"}

# Prompt size limit per step
MAX_STEP_PROMPT_CHARS = 2000


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
            }
            for s in config.steps
        ],
    }
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def validate_config(config: WorkflowConfig) -> list[str]:
    """Deterministic validation of a WorkflowConfig.

    Returns a list of error strings.  Empty list = valid.
    Checks: YAML structure, step IDs unique, tools legal, inputs
    reference existing steps, DAG is acyclic, prompts non-empty.
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
        if len(step.prompt) > MAX_STEP_PROMPT_CHARS:
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
# ConfigMutator
# ------------------------------------------------------------------

class ConfigMutator:
    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    # ------------------------------------------------------------------
    # Reflective self-rewrite
    # ------------------------------------------------------------------

    def reflective_self_rewrite(
        self,
        config: WorkflowConfig,
        action_history: list[ActionRecord],
        score: float,
    ) -> WorkflowConfig:
        """Improve step prompts based on real execution traces.

        Two-pass approach:
          1. Trace → abstract experience summary
          2. Config + summary + score → improved config (with validate-retry loop)

        Falls back to the original config if all retries fail.
        """
        from midas_agent.prompts import (
            CONFIG_CREATION_SUMMARIZE_PROMPT,
            REFLECTIVE_MUTATION_PROMPT,
        )
        from midas_agent.workspace.config_evolution.config_creator import (
            format_trace,
            _extract_yaml,
            _parse_config_yaml,
            _tool_usage_summary,
        )

        if not action_history:
            return config

        # -- Pass 1: trace → abstract summary --
        formatted = format_trace(action_history)
        tool_summary = _tool_usage_summary(action_history)

        summarize_prompt = CONFIG_CREATION_SUMMARIZE_PROMPT.format(
            iteration_count=len(action_history),
            score=score,
            formatted_trace=formatted,
            tool_usage_summary=tool_summary,
        )

        try:
            resp = self._system_llm(
                LLMRequest(messages=[{"role": "user", "content": summarize_prompt}],
                           model="default"),
            )
        except Exception as e:
            logger.warning("Reflective mutation pass 1 failed: %s", e)
            return config

        summary = (resp.content or "").strip()
        if not summary:
            logger.warning("Reflective mutation pass 1 returned empty summary")
            return config

        # -- Pass 2: config + summary + score → improved config --
        config_yaml = _config_to_yaml(config)

        mutate_prompt = REFLECTIVE_MUTATION_PROMPT.format(
            config_yaml=config_yaml,
            summary=summary,
            score=score,
        )

        # Build conversation for the validate-retry loop
        messages = [{"role": "user", "content": mutate_prompt}]

        for attempt in range(1 + MAX_VALIDATION_RETRIES):
            try:
                resp = self._system_llm(
                    LLMRequest(messages=messages, model="default"),
                )
            except Exception as e:
                logger.warning("Reflective mutation pass 2 failed: %s", e)
                return config

            raw_yaml = _extract_yaml(resp.content or "")
            if not raw_yaml:
                logger.warning("Reflective mutation: empty response (attempt %d)", attempt + 1)
                return config

            new_config = _parse_config_yaml(raw_yaml)
            if new_config is None:
                # YAML didn't parse — ask LLM to fix
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": "That YAML failed to parse. Please output valid YAML."})
                continue

            # Deterministic validation
            validation_errors = validate_config(new_config)
            if not validation_errors:
                logger.info(
                    "Reflective mutation accepted for '%s' (attempt %d)",
                    config.meta.name, attempt + 1,
                )
                return new_config

            # Feed errors back to the LLM for retry
            error_msg = (
                "The configuration has validation errors:\n"
                + "\n".join(f"- {e}" for e in validation_errors)
                + "\n\nPlease fix these errors and output the corrected YAML."
            )
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append({"role": "user", "content": error_msg})
            logger.info(
                "Reflective mutation: %d validation errors, retrying (attempt %d/%d)",
                len(validation_errors), attempt + 1, 1 + MAX_VALIDATION_RETRIES,
            )

        logger.warning("Reflective mutation: exhausted retries, keeping original config")
        return config

    # ------------------------------------------------------------------
    # Legacy reproduce (for eviction path — kept for backward compat)
    # ------------------------------------------------------------------

    def reproduce(
        self,
        base_config: WorkflowConfig,
        summaries: list[str],
    ) -> dict:
        """Create a new config variant based on the base config and episode summaries."""
        steps_repr = []
        for step in base_config.steps:
            steps_repr.append({
                "id": step.id,
                "prompt": step.prompt,
                "tools": step.tools,
                "inputs": step.inputs,
            })

        prompt = (
            "You are a configuration evolution engine. Given the base workflow "
            "configuration and summaries of past episodes, create a new variant "
            "configuration that improves upon the base.\n\n"
            f"Base config name: {base_config.meta.name}\n"
            f"Base config description: {base_config.meta.description}\n"
            f"Steps: {json.dumps(steps_repr)}\n\n"
            f"Episode summaries:\n"
            + "\n".join(f"- {s}" for s in summaries)
            + "\n\nRespond with a JSON object representing the new configuration."
        )

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model="default",
        )
        response = self._system_llm(request)

        try:
            result = json.loads(response.content or "{}")
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "meta": {
                "name": base_config.meta.name + "_variant",
                "description": base_config.meta.description,
            },
            "steps": steps_repr,
        }

    def self_rewrite(
        self,
        config: WorkflowConfig,
        summary: str,
    ) -> WorkflowConfig:
        """Legacy self-rewrite (plain LLM, no trace feedback).

        Kept as fallback when no action_history is available.
        """
        steps_repr = []
        for step in config.steps:
            steps_repr.append({
                "id": step.id,
                "prompt": step.prompt,
                "tools": step.tools,
                "inputs": step.inputs,
            })

        prompt = (
            "You are a configuration evolution engine. Given the current workflow "
            "configuration and an episode summary, rewrite the configuration to "
            "improve it.\n\n"
            f"Current config name: {config.meta.name}\n"
            f"Current config description: {config.meta.description}\n"
            f"Steps: {json.dumps(steps_repr)}\n\n"
            f"Episode summary: {summary}\n\n"
            "Respond with a JSON object representing the rewritten configuration."
        )

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model="default",
        )
        response = self._system_llm(request)

        try:
            data = json.loads(response.content or "{}")
            if isinstance(data, dict) and "steps" in data:
                meta_data = data.get("meta", {})
                new_meta = ConfigMeta(
                    name=meta_data.get("name", config.meta.name),
                    description=meta_data.get("description", config.meta.description),
                )
                new_steps = []
                for s in data["steps"]:
                    new_steps.append(StepConfig(
                        id=s.get("id", "step"),
                        prompt=s.get("prompt", ""),
                        tools=s.get("tools", []),
                        inputs=s.get("inputs", []),
                    ))
                return WorkflowConfig(meta=new_meta, steps=new_steps)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        # Fallback: return a copy of the config with an updated description.
        new_meta = ConfigMeta(
            name=config.meta.name,
            description=config.meta.description + " (rewritten)",
        )
        new_steps = [
            StepConfig(
                id=step.id,
                prompt=step.prompt,
                tools=list(step.tools),
                inputs=list(step.inputs),
            )
            for step in config.steps
        ]
        return WorkflowConfig(meta=new_meta, steps=new_steps)
