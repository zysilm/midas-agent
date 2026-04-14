"""Unit tests for ConfigMutator."""
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.config_evolution.mutator import ConfigMutator
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


@pytest.mark.unit
class TestConfigMutator:
    """Tests for the ConfigMutator class."""

    def _make_system_llm(self, content: str = '{"steps": []}'):
        """Create a fake system_llm callback."""
        return MagicMock(
            return_value=LLMResponse(
                content=content,
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        )

    def _make_base_config(self) -> WorkflowConfig:
        """Create a simple base WorkflowConfig for testing."""
        meta = ConfigMeta(name="base", description="base config")
        step = StepConfig(id="s1", prompt="do something", tools=["bash"])
        return WorkflowConfig(meta=meta, steps=[step])

    def test_construction(self):
        """ConfigMutator can be constructed with a system_llm callback."""
        system_llm = self._make_system_llm()
        mutator = ConfigMutator(system_llm=system_llm)

        assert mutator is not None

    def test_reproduce_returns_dict(self):
        """reproduce() returns a dict representing a new configuration."""
        system_llm = self._make_system_llm()
        mutator = ConfigMutator(system_llm=system_llm)
        base_config = self._make_base_config()

        result = mutator.reproduce(base_config, summaries=["step worked well"])

        assert isinstance(result, dict)

    def test_self_rewrite_returns_config(self):
        """self_rewrite() returns a WorkflowConfig instance."""
        system_llm = self._make_system_llm()
        mutator = ConfigMutator(system_llm=system_llm)
        base_config = self._make_base_config()

        result = mutator.self_rewrite(base_config, summary="improved performance")

        assert isinstance(result, WorkflowConfig)

    def test_reproduce_uses_system_llm(self):
        """reproduce() calls the system_llm callback internally."""
        system_llm = self._make_system_llm()
        mutator = ConfigMutator(system_llm=system_llm)
        base_config = self._make_base_config()

        mutator.reproduce(base_config, summaries=["observation one"])

        assert system_llm.call_count >= 1
