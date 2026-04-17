"""End-to-end tests for skill evolution across multiple episodes.

Tests the complete lifecycle: spawn -> create skill -> evolve -> export -> import.
Tests are expected to FAIL until skill evolution is fully implemented.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.inference.schemas import GraphEmergenceArtifact
from midas_agent.llm.types import LLMResponse, TokenUsage
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer


# ===================================================================
# Helpers
# ===================================================================


def _skill_json(name: str, desc: str, content: str) -> str:
    return json.dumps({"name": name, "description": desc, "content": content})


def _make_system_llm(responses: list[str]):
    idx = {"i": 0}
    def fake(request):
        r = responses[min(idx["i"], len(responses) - 1)]
        idx["i"] += 1
        return LLMResponse(content=r, tool_calls=None, usage=TokenUsage(30, 30))
    return fake


def _make_action_history() -> list[ActionRecord]:
    return [
        ActionRecord(action_name="search_code", arguments={"pattern": "bug"}, result="Found", timestamp=1.0),
        ActionRecord(action_name="edit_file", arguments={"path": "fix.py"}, result="OK", timestamp=2.0),
    ]


def _mock_gepa(evolved_text: str):
    mock_optimized = MagicMock()
    mock_optimized.skill_text = evolved_text
    mock_optimizer = MagicMock()
    mock_optimizer.compile.return_value = mock_optimized
    return mock_optimizer


def _make_reviewer(system_llm, manager, enabled=True):
    return SkillReviewer(
        system_llm=system_llm,
        free_agent_manager=manager,
        skill_evolution=enabled,
    )


def _make_manager():
    pricing = MagicMock()
    pricing.calculate_price.return_value = 100
    return FreeAgentManager(pricing_engine=pricing)


# ===================================================================
# Full lifecycle E2E
# ===================================================================


@pytest.mark.integration
class TestSkillEvolutionE2E:
    """End-to-end skill evolution across multiple episodes."""

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_full_lifecycle(self, mock_dspy):
        """Episode 1: create -> Episode 2: evolve -> Episode 3: evolve again.
        Verify skill content changes each time and constraints are respected."""
        system_llm = _make_system_llm([
            _skill_json("debug", "Debugging", "V1: Basic debugging with print."),
        ])
        manager = _make_manager()
        reviewer = _make_reviewer(system_llm, manager)

        agent = Agent(
            agent_id="evolving-agent",
            soul=Soul(system_prompt="You are a debugging specialist."),
            agent_type="free",
            skill=None,
        )
        manager.register(agent)

        # Episode 1: creation (skill=None, S_exec > 0)
        reviewer.review(
            agent=agent,
            eval_results={"ws-0": {"s_exec": 0.6, "s_w": 0.6}},
            action_history=_make_action_history(),
        )
        assert agent.skill is not None
        v1 = agent.skill.content
        assert "V1" in v1

        # Episode 2: evolution
        mock_dspy.GEPA.return_value = _mock_gepa("V2: Use logging, check stack traces.")
        reviewer.review(
            agent=agent,
            eval_results={"ws-0": {"s_exec": 0.75, "s_w": 0.75}},
            action_history=_make_action_history(),
        )
        v2 = agent.skill.content
        assert v2 != v1
        assert "V2" in v2

        # Episode 3: further evolution
        mock_dspy.GEPA.return_value = _mock_gepa("V3: Prioritize stack trace, then logging.")
        reviewer.review(
            agent=agent,
            eval_results={"ws-0": {"s_exec": 0.85, "s_w": 0.85}},
            action_history=_make_action_history(),
        )
        v3 = agent.skill.content
        assert v3 != v2
        assert "V3" in v3

        # All versions within constraint
        for v in [v1, v2, v3]:
            assert len(v) <= 5000

    def test_disabled_no_changes(self):
        """skill_evolution=False -> multi-episode, all skills remain None."""
        system_llm = MagicMock()
        manager = _make_manager()
        reviewer = _make_reviewer(system_llm, manager, enabled=False)

        agents = [
            Agent(agent_id=f"agent-{i}", soul=Soul(system_prompt="test"), agent_type="free")
            for i in range(3)
        ]
        for a in agents:
            manager.register(a)

        # Run 3 episodes
        for _ in range(3):
            for a in agents:
                reviewer.review(
                    agent=a,
                    eval_results={"ws-0": {"s_exec": 0.8, "s_w": 0.8}},
                    action_history=_make_action_history(),
                )

        # All skills still None
        for a in agents:
            assert a.skill is None
        system_llm.assert_not_called()

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_two_agents_evolve_independently(self, mock_dspy):
        """Two agents get different skills based on their histories."""
        manager = _make_manager()

        agent_a = Agent(
            agent_id="agent-A", soul=Soul(system_prompt="A"), agent_type="free",
        )
        agent_b = Agent(
            agent_id="agent-B", soul=Soul(system_prompt="B"), agent_type="free",
        )
        manager.register(agent_a)
        manager.register(agent_b)

        # Different SystemLLM responses for each agent
        call_count = {"n": 0}
        def system_llm(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                content = _skill_json("search", "Code search", "Agent A: use grep")
            else:
                content = _skill_json("edit", "Code editing", "Agent B: use sed")
            return LLMResponse(content=content, tool_calls=None, usage=TokenUsage(30, 30))

        reviewer = _make_reviewer(system_llm, manager)

        # Episode 1: both get different initial skills
        reviewer.review(agent=agent_a, eval_results={"ws-0": {"s_exec": 0.7, "s_w": 0.7}}, action_history=_make_action_history())
        reviewer.review(agent=agent_b, eval_results={"ws-0": {"s_exec": 0.6, "s_w": 0.6}}, action_history=_make_action_history())

        assert agent_a.skill.name != agent_b.skill.name
        assert agent_a.skill.content != agent_b.skill.content

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_evolved_skill_survives_artifact(self, mock_dspy):
        """Evolved skill persists through artifact export -> import cycle."""
        system_llm = _make_system_llm([
            _skill_json("debug", "Debug specialist", "Evolved: check logs then fix."),
        ])
        manager = _make_manager()
        reviewer = _make_reviewer(system_llm, manager)

        agent = Agent(
            agent_id="persistent-agent",
            soul=Soul(system_prompt="Debug agent"),
            agent_type="free",
            skill=None,
        )
        manager.register(agent)

        # Create skill
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.8, "s_w": 0.8}}, action_history=_make_action_history())
        assert agent.skill is not None

        # Export to artifact
        responsible = Agent(
            agent_id="resp", soul=Soul(system_prompt="Coordinator"),
            agent_type="workspace_bound",
        )
        artifact = GraphEmergenceArtifact(
            responsible_agent=responsible,
            free_agents=[agent],
            agent_prices={"persistent-agent": 100},
            agent_bankruptcy_rates={"persistent-agent": 0.0},
            budget_hint=50000,
        )

        # Roundtrip through JSON
        json_str = artifact.model_dump_json()
        restored = GraphEmergenceArtifact.model_validate_json(json_str)

        # Skill survives
        restored_agent = restored.free_agents[0]
        assert restored_agent.skill is not None
        assert restored_agent.skill.name == "debug"
        assert "check logs" in restored_agent.skill.content

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_graceful_on_gepa_failures(self, mock_dspy):
        """GEPA fails on some episodes -> agent keeps old skill, training continues."""
        system_llm = _make_system_llm([_skill_json("s", "d", "Initial.")])
        manager = _make_manager()
        reviewer = _make_reviewer(system_llm, manager)

        agent = Agent(
            agent_id="resilient", soul=Soul(system_prompt="test"),
            agent_type="free", skill=None,
        )
        manager.register(agent)

        # Episode 1: create skill
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.6, "s_w": 0.6}}, action_history=_make_action_history())
        assert agent.skill is not None
        original = agent.skill.content

        # Episode 2: GEPA fails
        mock_dspy.GEPA.return_value.compile.side_effect = RuntimeError("API error")
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.7, "s_w": 0.7}}, action_history=_make_action_history())
        assert agent.skill.content == original  # unchanged

        # Episode 3: GEPA succeeds again
        mock_dspy.GEPA.return_value.compile.side_effect = None
        mock_dspy.GEPA.return_value = _mock_gepa("Recovered and improved.")
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.8, "s_w": 0.8}}, action_history=_make_action_history())
        assert agent.skill.content != original  # evolved

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_constraint_rejections_across_episodes(self, mock_dspy):
        """Some evolutions rejected (too long), agent continues with old skill."""
        system_llm = _make_system_llm([_skill_json("s", "d", "Short initial.")])
        manager = _make_manager()
        reviewer = _make_reviewer(system_llm, manager)

        agent = Agent(
            agent_id="constrained", soul=Soul(system_prompt="test"),
            agent_type="free", skill=None,
        )
        manager.register(agent)

        # Episode 1: create
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.5, "s_w": 0.5}}, action_history=_make_action_history())
        v1 = agent.skill.content

        # Episode 2: GEPA produces too-long result -> rejected
        mock_dspy.GEPA.return_value = _mock_gepa("x" * 6000)
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.7, "s_w": 0.7}}, action_history=_make_action_history())
        assert agent.skill.content == v1  # unchanged

        # Episode 3: GEPA produces good result -> accepted
        mock_dspy.GEPA.return_value = _mock_gepa("V2 concise improvement.")
        reviewer.review(agent=agent, eval_results={"ws-0": {"s_exec": 0.8, "s_w": 0.8}}, action_history=_make_action_history())
        assert agent.skill.content != v1  # updated
        assert agent.skill.content == "V2 concise improvement."
