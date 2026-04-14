"""Unit tests for production mode Pydantic schemas."""
import json

import pytest

from midas_agent.inference.schemas import (
    FreeAgentSchema,
    GraphEmergenceArtifact,
    ResponsibleAgentSchema,
    SkillSchema,
    SoulSchema,
)


@pytest.mark.unit
class TestSoulSchema:
    def test_roundtrip(self):
        soul = SoulSchema(system_prompt="You are an expert.")
        data = soul.model_dump()
        restored = SoulSchema.model_validate(data)
        assert restored.system_prompt == "You are an expert."


@pytest.mark.unit
class TestSkillSchema:
    def test_roundtrip(self):
        skill = SkillSchema(name="debug", description="Debugging", content="Steps...")
        data = skill.model_dump()
        restored = SkillSchema.model_validate(data)
        assert restored.name == "debug"
        assert restored.description == "Debugging"
        assert restored.content == "Steps..."


@pytest.mark.unit
class TestFreeAgentSchema:
    def test_roundtrip_with_skill(self):
        fa = FreeAgentSchema(
            agent_id="fa-1",
            soul=SoulSchema(system_prompt="prompt"),
            skill=SkillSchema(name="s", description="d", content="c"),
            price=1000,
            bankruptcy_rate=0.1,
        )
        data = fa.model_dump()
        restored = FreeAgentSchema.model_validate(data)
        assert restored.agent_id == "fa-1"
        assert restored.price == 1000
        assert restored.bankruptcy_rate == 0.1
        assert restored.skill is not None

    def test_roundtrip_without_skill(self):
        fa = FreeAgentSchema(
            agent_id="fa-2",
            soul=SoulSchema(system_prompt="prompt"),
            price=500,
            bankruptcy_rate=0.0,
        )
        restored = FreeAgentSchema.model_validate(fa.model_dump())
        assert restored.skill is None

    def test_bankruptcy_rate_validation(self):
        with pytest.raises(Exception):
            FreeAgentSchema(
                agent_id="fa-3",
                soul=SoulSchema(system_prompt="p"),
                price=100,
                bankruptcy_rate=1.5,
            )


@pytest.mark.unit
class TestGraphEmergenceArtifact:
    def _make_artifact(self) -> GraphEmergenceArtifact:
        return GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="responsible"),
            ),
            free_agents=[
                FreeAgentSchema(
                    agent_id="fa-1",
                    soul=SoulSchema(system_prompt="free-1"),
                    skill=SkillSchema(name="nav", description="code nav", content="..."),
                    price=1250,
                    bankruptcy_rate=0.05,
                ),
                FreeAgentSchema(
                    agent_id="fa-2",
                    soul=SoulSchema(system_prompt="free-2"),
                    price=3400,
                    bankruptcy_rate=0.30,
                ),
            ],
            budget_hint=58000,
        )

    def test_json_roundtrip(self):
        artifact = self._make_artifact()
        json_str = artifact.model_dump_json(indent=2)
        restored = GraphEmergenceArtifact.model_validate_json(json_str)
        assert restored.budget_hint == 58000
        assert len(restored.free_agents) == 2
        assert restored.responsible_agent.soul.system_prompt == "responsible"

    def test_free_agents_preserve_order(self):
        artifact = self._make_artifact()
        restored = GraphEmergenceArtifact.model_validate(artifact.model_dump())
        assert restored.free_agents[0].agent_id == "fa-1"
        assert restored.free_agents[1].agent_id == "fa-2"

    def test_empty_free_agents(self):
        artifact = GraphEmergenceArtifact(
            responsible_agent=ResponsibleAgentSchema(
                soul=SoulSchema(system_prompt="solo"),
            ),
            budget_hint=10000,
        )
        assert len(artifact.free_agents) == 0
        restored = GraphEmergenceArtifact.model_validate_json(artifact.model_dump_json())
        assert len(restored.free_agents) == 0
