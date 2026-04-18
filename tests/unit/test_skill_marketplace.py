"""Unit tests for skill marketplace display in EnvironmentContext.

Tests verify that agents with skills show skill names, agents without
skills show 'general', and skill changes are reflected in context.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from midas_agent.context.environment import EnvironmentContext
from midas_agent.llm.types import LLMResponse, TokenUsage
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(agent_id: str = "fa-1", skill: Skill | None = None) -> Agent:
    return Agent(
        agent_id=agent_id,
        soul=Soul(system_prompt=f"You are agent {agent_id}."),
        agent_type="free",
        skill=skill,
    )


def _build_env_context(fam: FreeAgentManager) -> EnvironmentContext:
    """Build EnvironmentContext from a FreeAgentManager, mirroring workspace.py logic."""
    agent_lines = []
    agents = fam.free_agents
    for agent_id, agent in agents.items():
        price = fam._pricing_engine.calculate_price(agent)
        skill_name = agent.skill.name if agent.skill else "general"
        agent_lines.append(f"{agent_id}: {skill_name} (price={price})")

    return EnvironmentContext(
        cwd="/testbed",
        shell="bash",
        current_date=str(date.today()),
        balance=100000,
        available_agents=agent_lines,
    )


# ===========================================================================
# 12. Agent with skill shows skill name
# ===========================================================================


@pytest.mark.unit
class TestAgentWithSkill:
    """Agent with Skill(name='debug_expert') shows 'debug_expert' in XML."""

    def test_skill_name_in_xml(self):
        pricing = MagicMock()
        pricing.calculate_price.return_value = 500
        fam = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent(
            "expert-1",
            skill=Skill(name="debug_expert", description="Expert debugger", content="..."),
        )
        fam.register(agent)

        ctx = _build_env_context(fam)
        xml = ctx.serialize_to_xml()

        assert "debug_expert" in xml


# ===========================================================================
# 13. Agent without skill shows "general"
# ===========================================================================


@pytest.mark.unit
class TestAgentWithoutSkill:
    """Agent with skill=None shows 'general' in XML output."""

    def test_general_label_in_xml(self):
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        fam = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent("newbie-1", skill=None)
        fam.register(agent)

        ctx = _build_env_context(fam)
        xml = ctx.serialize_to_xml()

        assert "general" in xml


# ===========================================================================
# 14. Skill appears after post_episode
# ===========================================================================


@pytest.mark.unit
class TestSkillAppearsAfterPostEpisode:
    """Simulate full flow -- agent starts with skill=None, post_episode
    creates skill, verify EnvironmentContext changes."""

    def test_env_context_changes_after_skill_creation(self):
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        fam = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent("fa-1", skill=None)
        fam.register(agent)

        # Before: should show "general"
        ctx_before = _build_env_context(fam)
        xml_before = ctx_before.serialize_to_xml()
        assert "general" in xml_before
        assert "code_nav" not in xml_before

        # Simulate skill creation (as SkillReviewer would do)
        system_llm = MagicMock(return_value=LLMResponse(
            content=json.dumps({
                "name": "code_nav",
                "description": "Code navigation expert",
                "content": "Use grep and find to navigate...",
            }),
            tool_calls=None,
            usage=TokenUsage(input_tokens=30, output_tokens=30),
        ))
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=fam,
            skill_evolution=True,
        )

        history = [
            ActionRecord(
                action_name="search_code",
                arguments={"pattern": "def main"},
                result="Found in main.py",
                timestamp=1.0,
            ),
        ]

        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.9},
            action_history=history,
        )

        # After: should show "code_nav" instead of "general"
        ctx_after = _build_env_context(fam)
        xml_after = ctx_after.serialize_to_xml()
        assert "code_nav" in xml_after
