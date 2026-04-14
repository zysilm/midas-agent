"""Config mutator — reproduction and self-rewrite via SystemLLM."""
from __future__ import annotations

from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.workspace.config_evolution.config_schema import WorkflowConfig


class ConfigMutator:
    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        raise NotImplementedError

    def reproduce(
        self,
        base_config: WorkflowConfig,
        summaries: list[str],
    ) -> dict:
        raise NotImplementedError

    def self_rewrite(
        self,
        config: WorkflowConfig,
        summary: str,
    ) -> WorkflowConfig:
        raise NotImplementedError
