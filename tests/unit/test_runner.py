"""Unit tests for the unified production inference runner."""
import json
import os
import tempfile

import pytest

from midas_agent.inference.runner import run_inference, _build_market_info
from midas_agent.inference.schemas import (
    FreeAgentSchema,
    GraphEmergenceArtifact,
    ResponsibleAgentSchema,
    SkillSchema,
    SoulSchema,
)
from midas_agent.llm.types import LLMResponse, TokenUsage
from midas_agent.stdlib.action import ActionRegistry
from midas_agent.types import Issue

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def _make_issue() -> Issue:
    return Issue(issue_id="test-1", repo="test/repo", description="Fix the bug")


def _make_action_registry() -> ActionRegistry:
    return ActionRegistry(actions=[])


@pytest.mark.unit
class TestRunInference:
    def test_rejects_unknown_extension(self, fake_llm_provider):
        with pytest.raises(ValueError, match="Unsupported"):
            run_inference(
                "config.txt", _make_issue(), fake_llm_provider, _make_action_registry(),
            )

    @pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
    def test_config_evolution_basic(self, fake_llm_provider):
        """Config evolution mode loads YAML and runs DAGExecutor."""
        config = {
            "meta": {"name": "test", "description": "test config"},
            "steps": [
                {
                    "id": "solve",
                    "prompt": "Fix the issue.",
                    "tools": [],
                    "inputs": [],
                },
            ],
        }

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(config, f)
            config_path = f.name

        try:
            result = run_inference(
                config_path, _make_issue(), fake_llm_provider, _make_action_registry(),
            )
            # fake_llm_provider returns "test response" which becomes the patch
            assert result is not None
        finally:
            os.unlink(config_path)

    def test_graph_emergence_basic(self, fake_llm_provider):
        """Graph emergence mode loads JSON artifact and runs PlanExecuteAgent."""
        artifact = GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="You are a coordinator."),
            ),
            free_agents=[
                FreeAgentSchema(
                    agent_id="fa-1",
                    soul=SoulSchema(system_prompt="test agent"),
                    skill=SkillSchema(name="debug", description="debugging", content="..."),
                    price=500,
                    bankruptcy_rate=0.1,
                ),
            ],
            budget_hint=10000,
        )

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write(artifact.model_dump_json())
            config_path = f.name

        try:
            result = run_inference(
                config_path, _make_issue(), fake_llm_provider, _make_action_registry(),
            )
            # PlanExecuteAgent will call LLM for planning then execution
            assert result is not None
        finally:
            os.unlink(config_path)

    def test_graph_emergence_respects_budget_override(self):
        """User-provided budget overrides budget_hint from artifact."""
        from tests.unit.conftest import FakeLLMProvider

        # Provider that returns enough tokens to blow a tiny budget
        provider = FakeLLMProvider(responses=[
            LLMResponse(content="plan", tool_calls=None, usage=TokenUsage(50, 50)),
        ])

        artifact = GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="coord"),
            ),
            budget_hint=100000,
        )

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write(artifact.model_dump_json())
            config_path = f.name

        try:
            # budget=10 overrides budget_hint=100000; first call uses 100 tokens
            # which exceeds budget, so second call in execution phase fails
            result = run_inference(
                config_path, _make_issue(), provider, _make_action_registry(),
                budget=10,
            )
            # Should return None because budget was exhausted
            assert result is None
        finally:
            os.unlink(config_path)


@pytest.mark.unit
class TestBuildMarketInfo:
    def test_includes_agent_info(self):
        artifact = GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="coord"),
            ),
            free_agents=[
                FreeAgentSchema(
                    agent_id="fa-1",
                    soul=SoulSchema(system_prompt="test"),
                    skill=SkillSchema(name="debug", description="debugging code", content="..."),
                    price=1000,
                    bankruptcy_rate=0.05,
                ),
            ],
            budget_hint=50000,
        )
        info = _build_market_info(artifact)
        assert "fa-1" in info
        assert "debugging code" in info
        assert "price=1000" in info
        assert "5%" in info

    def test_handles_no_skill(self):
        artifact = GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="coord"),
            ),
            free_agents=[
                FreeAgentSchema(
                    agent_id="fa-2",
                    soul=SoulSchema(system_prompt="test"),
                    price=500,
                    bankruptcy_rate=0.0,
                ),
            ],
            budget_hint=10000,
        )
        info = _build_market_info(artifact)
        assert "no skill" in info

    def test_empty_agents(self):
        artifact = GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="coord"),
            ),
            budget_hint=10000,
        )
        info = _build_market_info(artifact)
        assert "Available agents:" in info
