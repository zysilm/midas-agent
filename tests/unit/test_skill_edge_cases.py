"""Unit tests for skill pipeline edge cases.

Tests verify graceful handling of exceptions, long histories,
and agents with None skills and bankruptcy data.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from midas_agent.context.environment import EnvironmentContext
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.skill_evolution import create_initial_skill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(agent_id: str = "fa-1", skill: Skill | None = None) -> Agent:
    return Agent(
        agent_id=agent_id,
        soul=Soul(system_prompt=f"Agent {agent_id}."),
        agent_type="free",
        skill=skill,
    )


# ===========================================================================
# 21. System LLM raises exception
# ===========================================================================


@pytest.mark.unit
class TestSystemLLMException:
    """SkillReviewer handles system LLM exceptions gracefully."""

    def test_system_llm_raises_no_crash(self):
        def exploding_llm(request):
            raise RuntimeError("LLM service unavailable")

        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=exploding_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )

        agent = _make_agent(skill=None)
        history = [
            ActionRecord(action_name="bash", arguments={"command": "ls"},
                         result="main.py", timestamp=1.0),
        ]

        # Should not raise
        reviewer.review(agent, {"s_exec": 0.9}, history)
        assert agent.skill is None

    def test_create_initial_skill_exception_returns_none(self):
        """create_initial_skill should return None when system LLM raises."""

        def exploding_llm(request):
            raise ValueError("Parse error")

        result = create_initial_skill(
            system_llm=exploding_llm,
            action_history=[
                ActionRecord(action_name="bash", arguments={},
                             result="ok", timestamp=1.0),
            ],
            eval_results={"s_exec": 0.9},
        )

        assert result is None


# ===========================================================================
# 22. Very long action history
# ===========================================================================


@pytest.mark.unit
class TestLongActionHistory:
    """50 actions should not crash create_initial_skill."""

    def test_50_actions_no_crash(self):
        history = [
            ActionRecord(
                action_name=f"action_{i}",
                arguments={"arg": f"value_{i}"},
                result=f"Result of action {i} " * 20,  # ~400 chars each
                timestamp=float(i),
            )
            for i in range(50)
        ]

        def system_llm(request):
            return LLMResponse(
                content=json.dumps({
                    "name": "heavy_lifter",
                    "description": "Handles long sequences",
                    "content": "Process many steps systematically.",
                }),
                tool_calls=None,
                usage=TokenUsage(input_tokens=500, output_tokens=200),
            )

        result = create_initial_skill(
            system_llm=system_llm,
            action_history=history,
            eval_results={"s_exec": 0.8},
        )

        assert result is not None
        assert result.name == "heavy_lifter"


# ===========================================================================
# 23. Agent with skill=None and bankruptcy > 0
# ===========================================================================


@pytest.mark.unit
class TestAgentNullSkillWithBankruptcy:
    """Agent with skill=None and non-zero bankruptcy shows correctly
    in EnvironmentContext."""

    def test_null_skill_agent_with_bankruptcy_displays(self):
        pricing = MagicMock()
        pricing.calculate_price.return_value = 200
        fam = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent("risky-1", skill=None)
        fam.register(agent)

        # Build context like workspace.py does
        agent_lines = []
        agents = fam.free_agents
        for agent_id, a in agents.items():
            price = fam._pricing_engine.calculate_price(a)
            skill_name = a.skill.name if a.skill else "general"
            br = 0.5  # simulated bankruptcy
            agent_lines.append(
                f"{agent_id}: {skill_name} (price={price}, bankruptcy={br:.2f})"
            )

        ctx = EnvironmentContext(
            cwd="/testbed",
            shell="bash",
            current_date=str(date.today()),
            balance=50000,
            available_agents=agent_lines,
        )
        xml = ctx.serialize_to_xml()

        assert "risky-1" in xml
        assert "general" in xml
        assert "bankruptcy=0.50" in xml
