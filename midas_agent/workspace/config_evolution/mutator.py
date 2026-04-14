"""Config mutator — reproduction and self-rewrite via SystemLLM."""
from __future__ import annotations

import json
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)


class ConfigMutator:
    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    def reproduce(
        self,
        base_config: WorkflowConfig,
        summaries: list[str],
    ) -> dict:
        """Create a new config variant based on the base config and episode summaries.

        Calls system_llm to generate a new configuration, then parses the
        response into a dict.  Falls back to a simple dict derived from the
        base config when parsing fails.
        """
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

        # Try to parse the LLM response as JSON; fall back to a simple dict.
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
        """Rewrite the current config based on an episode summary.

        Calls system_llm to propose improvements, then returns a new
        WorkflowConfig instance (possibly identical to the input if the LLM
        response cannot be parsed into a meaningful update).
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

        # Try to parse the LLM response and build a new WorkflowConfig.
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
