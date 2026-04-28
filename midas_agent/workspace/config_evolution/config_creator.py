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
                id=str(s.get("id", f"step_{len(steps)}")),
                prompt=s.get("prompt", ""),
                tools=s.get("tools", []),
                inputs=[str(i) for i in s.get("inputs", [])],
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

        summary = ""
        for attempt in range(3):
            try:
                resp = self._system_llm(
                    LLMRequest(messages=[{"role": "user", "content": summarize_prompt}],
                               model="default"),
                )
                summary = (resp.content or "").strip()
                if summary:
                    break
                logger.warning("Config creation pass 1 returned empty (attempt %d/3)", attempt + 1)
            except Exception as e:
                logger.warning("Config creation pass 1 API error (attempt %d/3): %s", attempt + 1, e)

        if not summary:
            logger.warning("Config creation pass 1 failed after 3 attempts")
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
                logger.warning("Config creation pass 2 API error (attempt %d/%d): %s",
                               attempt + 1, 1 + max_retries, e)
                continue

            raw_yaml = _extract_yaml(resp.content or "")
            if not raw_yaml:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": "Output the YAML inside ```yaml fences."})
                logger.info("Config creation: no YAML, retrying (attempt %d/%d)", attempt + 1, 1 + max_retries)
                continue

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

        # Build a plain-text description of each step for the LLM
        step_lines = []
        for step in base_config.steps:
            step_lines.append(f"=== STEP: {step.id} ===")
            step_lines.append(step.prompt.strip())
            step_lines.append("")
        steps_description = "\n".join(step_lines)

        merge_prompt = CONFIG_MERGE_PROMPT.format(
            steps_description=steps_description,
            issue_description=issue.description,
        )

        messages = [{"role": "user", "content": merge_prompt}]

        for attempt in range(1 + MAX_MERGE_RETRIES):
            resp = self._system_llm(
                LLMRequest(messages=messages, model="default"),
            )

            # Parse delimiter-formatted response: === STEP: <id> === sections
            parsed_prompts = self._parse_delimiter_response(
                resp.content or "", base_config,
            )

            if parsed_prompts is None:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user", "content": (
                    "Your response could not be parsed. Please use EXACTLY this format:\n\n"
                    "=== STEP: <step_id> ===\n<the new prompt for this step>\n\n"
                    "Output ALL steps. No explanation, no YAML, no code fences."
                )})
                logger.info(
                    "Config merge: parse failed, retrying (attempt %d/%d)",
                    attempt + 1, 1 + MAX_MERGE_RETRIES,
                )
                continue

            # Graft parsed prompts onto base config structure
            merged = self._graft_prompts(base_config, parsed_prompts)

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
    def _parse_delimiter_response(
        text: str,
        base_config: WorkflowConfig,
    ) -> dict[str, str] | None:
        """Parse '=== STEP: <id> ===' delimited response into {step_id: prompt}.

        Returns None if no steps could be parsed.
        """
        # Split on the delimiter pattern
        parts = re.split(r"===\s*STEP:\s*(\S+)\s*===", text)
        # parts[0] is preamble (ignored), then alternating (id, content) pairs
        if len(parts) < 3:
            return None

        prompts: dict[str, str] = {}
        for i in range(1, len(parts), 2):
            step_id = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                prompts[step_id] = content

        if not prompts:
            return None

        # Match parsed IDs to base config IDs (fuzzy: LLM may slightly alter IDs)
        # Convert to str — YAML may parse numeric IDs as int (e.g. id: 1)
        base_ids = [str(s.id) for s in base_config.steps]
        matched: dict[str, str] = {}
        for parsed_id, prompt in prompts.items():
            pid = str(parsed_id)
            if pid in base_ids:
                matched[pid] = prompt
            else:
                # Fuzzy match: find the closest base ID
                for base_id in base_ids:
                    if pid in base_id or base_id in pid:
                        matched[base_id] = prompt
                        break

        # Need at least one matched step
        if not matched:
            return None

        logger.info(
            "Config merge: parsed %d/%d steps from delimiter response",
            len(matched), len(base_ids),
        )
        return matched

    @staticmethod
    def _graft_prompts(
        base: WorkflowConfig,
        prompts: dict[str, str],
    ) -> WorkflowConfig:
        """Graft parsed prompts onto base config, keeping structure intact."""
        new_steps = []
        for step in base.steps:
            new_prompt = prompts.get(str(step.id), step.prompt)
            new_steps.append(StepConfig(
                id=step.id,
                prompt=new_prompt,
                tools=step.tools,
                inputs=step.inputs,
                goal=step.goal,
            ))
        return WorkflowConfig(meta=base.meta, steps=new_steps)

