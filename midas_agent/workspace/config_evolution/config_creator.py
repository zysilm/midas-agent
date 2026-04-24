"""Config creator and merger for Configuration Evolution workflows.

ConfigCreator: two-pass generation (trace → summary → YAML config).
ConfigMerger: merges a base DAG with an issue (rewrites step prompts
to embed issue context, preventing agent overscoping).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Callable

import yaml

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.types import Issue
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)

logger = logging.getLogger(__name__)

# Trace formatting limits
MAX_TRACE_ITERATIONS = 60
MAX_ARG_CHARS = 80
MAX_RESULT_CHARS = 150


# ------------------------------------------------------------------
# Trace formatting helpers
# ------------------------------------------------------------------

def format_trace(action_history: list[ActionRecord]) -> str:
    """Format action history into a compact, readable trace."""
    lines: list[str] = []
    for i, record in enumerate(action_history[:MAX_TRACE_ITERATIONS], 1):
        args_parts: list[str] = []
        for k, v in record.arguments.items():
            v_str = v if isinstance(v, str) else repr(v)
            if len(v_str) > MAX_ARG_CHARS:
                v_str = v_str[:MAX_ARG_CHARS] + "..."
            args_parts.append(f"{k}={v_str}")
        args_str = ", ".join(args_parts)

        result = record.result or "(empty)"
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "..."
        result = result.replace("\n", " ").strip()

        lines.append(f"[iter {i}] {record.action_name}({args_str}) → {result}")

    if len(action_history) > MAX_TRACE_ITERATIONS:
        lines.append(
            f"... ({len(action_history) - MAX_TRACE_ITERATIONS} more iterations truncated)"
        )
    return "\n".join(lines)


def _tool_usage_summary(action_history: list[ActionRecord]) -> str:
    """Summarise tool usage counts, e.g. 'bash (32), str_replace_editor (12)'."""
    counts = Counter(r.action_name for r in action_history)
    return ", ".join(f"{name} ({count})" for name, count in counts.most_common())


# ------------------------------------------------------------------
# YAML parsing helpers
# ------------------------------------------------------------------

def _extract_yaml(text: str) -> str:
    """Extract YAML from an LLM response that may include code fences."""
    match = re.search(r"```ya?ml\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _parse_config_yaml(yaml_text: str) -> WorkflowConfig | None:
    """Parse YAML text into a WorkflowConfig.  Returns None on failure."""
    try:
        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict) or "steps" not in data:
            return None

        meta_data = data.get("meta", {})
        meta = ConfigMeta(
            name=meta_data.get("name", "generated"),
            description=meta_data.get("description", "auto-generated from trace"),
        )

        steps: list[StepConfig] = []
        for s in data["steps"]:
            steps.append(StepConfig(
                id=s.get("id", f"step_{len(steps)}"),
                prompt=s.get("prompt", ""),
                tools=s.get("tools", []),
                inputs=s.get("inputs", []),
                goal=s.get("goal", ""),
            ))

        if not steps:
            return None
        return WorkflowConfig(meta=meta, steps=steps)
    except Exception as e:
        logger.warning("Failed to parse config YAML: %s", e)
        return None


# ------------------------------------------------------------------
# ConfigCreator
# ------------------------------------------------------------------

class ConfigCreator:
    """Generate workflow configs from successful execution traces."""

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    def create_config(
        self,
        action_history: list[ActionRecord],
        score: float,
    ) -> WorkflowConfig | None:
        """Two-pass config generation.  Returns None if either pass fails."""
        from midas_agent.prompts import (
            CONFIG_CREATION_SUMMARIZE_PROMPT,
            CONFIG_CREATION_GENERATE_PROMPT,
        )

        if not action_history:
            return None

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
            logger.warning("Config creation pass 1 failed: %s", e)
            return None

        summary = (resp.content or "").strip()
        if not summary:
            logger.warning("Config creation pass 1 returned empty summary")
            return None

        logger.info("Config creation pass 1 done (%d chars)", len(summary))

        # -- Pass 2: summary → YAML config (with validate-retry loop) --
        from midas_agent.workspace.config_evolution.mutator import validate_config

        generate_prompt = CONFIG_CREATION_GENERATE_PROMPT.format(
            score=score,
            summary=summary,
            iteration_count=len(action_history),
        )

        max_retries = 3
        messages = [{"role": "user", "content": generate_prompt}]

        for attempt in range(1 + max_retries):
            try:
                resp = self._system_llm(
                    LLMRequest(messages=messages, model="default"),
                )
            except Exception as e:
                logger.warning("Config creation pass 2 failed: %s", e)
                return None

            raw_yaml = _extract_yaml(resp.content or "")
            if not raw_yaml:
                logger.warning("Config creation pass 2 returned empty response")
                return None

            config = _parse_config_yaml(raw_yaml)
            if config is None:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": "That YAML failed to parse. Please output valid YAML."})
                continue

            validation_errors = validate_config(config)
            if not validation_errors:
                logger.info("Config created: '%s' (%d steps, attempt %d)", config.meta.name, len(config.steps), attempt + 1)
                return config

            error_msg = (
                "The configuration has validation errors:\n"
                + "\n".join(f"- {e}" for e in validation_errors)
                + "\n\nPlease fix these errors and output the corrected YAML."
            )
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append({"role": "user", "content": error_msg})
            logger.info("Config creation: %d errors, retrying (attempt %d/%d)", len(validation_errors), attempt + 1, 1 + max_retries)

        logger.warning("Config creation: exhausted retries")
        return None


# ------------------------------------------------------------------
# ConfigMerger
# ------------------------------------------------------------------

MAX_MERGE_RETRIES = 3


class ConfigMerger:
    """Merge a base DAG with an issue by embedding issue context into step prompts.

    The base DAG has generic step prompts (e.g., "Search the codebase for
    relevant files").  The merger rewrites each step prompt to include the
    relevant parts of the issue, so the agent doesn't receive the full issue
    as a separate message.

    This prevents overscoping: the localize step only knows the symptoms,
    not the reproduction code or expected fix.
    """

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self._system_llm = system_llm

    def merge(
        self,
        base_config: WorkflowConfig,
        issue: Issue,
    ) -> WorkflowConfig:
        """Merge issue context into base DAG step prompts.

        Raises RuntimeError if merging fails — there is no silent fallback.
        The agent cannot work without issue context in the step prompts.
        """
        from midas_agent.prompts import CONFIG_MERGE_PROMPT
        from midas_agent.workspace.config_evolution.mutator import (
            _config_to_yaml,
            validate_config,
        )

        base_yaml = _config_to_yaml(base_config)
        merge_prompt = CONFIG_MERGE_PROMPT.format(
            base_config_yaml=base_yaml,
            issue_description=issue.description,
        )

        messages = [{"role": "user", "content": merge_prompt}]

        for attempt in range(1 + MAX_MERGE_RETRIES):
            resp = self._system_llm(
                LLMRequest(messages=messages, model="default"),
            )

            raw_yaml = _extract_yaml(resp.content or "")
            if not raw_yaml:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": "You must output the YAML inside ```yaml fences. Please try again."})
                logger.info("Config merge: no YAML in response, retrying (attempt %d/%d)", attempt + 1, 1 + MAX_MERGE_RETRIES)
                continue

            merged = _parse_config_yaml(raw_yaml)
            if merged is None:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": "That YAML failed to parse. Please output valid YAML."})
                continue

            # Validate basic config structure (skip prompt length — merged
            # prompts are long because they embed the full issue text)
            validation_errors = validate_config(merged, skip_prompt_length=True)
            if validation_errors:
                error_msg = (
                    "The configuration has validation errors:\n"
                    + "\n".join(f"- {e}" for e in validation_errors)
                    + "\n\nPlease fix these errors and output the corrected YAML."
                )
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": error_msg})
                logger.info(
                    "Config merge: %d errors, retrying (attempt %d/%d)",
                    len(validation_errors), attempt + 1, 1 + MAX_MERGE_RETRIES,
                )
                continue

            # Structural check: same step IDs, tools, inputs
            if not self._structure_preserved(base_config, merged):
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "The merged config changed the DAG structure (step IDs, tools, "
                        "or inputs). You must keep these EXACTLY as the base config. "
                        "Only rewrite the prompt fields. Please try again."
                    ),
                })
                logger.info(
                    "Config merge: structure changed, retrying (attempt %d/%d)",
                    attempt + 1, 1 + MAX_MERGE_RETRIES,
                )
                continue

            # Verify prompts actually changed — issue must be embedded
            prompts_changed = any(
                b.prompt != m.prompt
                for b, m in zip(base_config.steps, merged.steps)
            )
            if not prompts_changed:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "The prompts are IDENTICAL to the base config. You MUST rewrite "
                        "each step's prompt to include the issue description. "
                        "Please try again."
                    ),
                })
                logger.info(
                    "Config merge: prompts unchanged, retrying (attempt %d/%d)",
                    attempt + 1, 1 + MAX_MERGE_RETRIES,
                )
                continue

            logger.info(
                "Config merged for issue '%s' (%d steps, attempt %d)",
                issue.issue_id, len(merged.steps), attempt + 1,
            )
            return merged

        raise RuntimeError(
            f"Config merge failed after {1 + MAX_MERGE_RETRIES} attempts "
            f"for issue '{issue.issue_id}'. Cannot proceed without issue context."
        )

    @staticmethod
    def _structure_preserved(
        base: WorkflowConfig,
        merged: WorkflowConfig,
    ) -> bool:
        """Check that merged config has same structure as base."""
        if len(base.steps) != len(merged.steps):
            return False
        for b, m in zip(base.steps, merged.steps):
            if b.id != m.id or b.tools != m.tools or b.inputs != m.inputs:
                return False
        return True
