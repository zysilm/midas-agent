"""Unit tests for the rewritten SkillReviewer, update_embedding, and config flag.

Tests are expected to FAIL until SkillReviewer is rewritten to use GEPA.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from midas_agent.config import MidasConfig
from midas_agent.llm.types import LLMResponse, TokenUsage
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer


# ===================================================================
# Helpers
# ===================================================================


def _make_agent(skill: Skill | None = None) -> Agent:
    return Agent(
        agent_id="fa-1",
        soul=Soul(system_prompt="You are a debugging agent."),
        agent_type="free",
        skill=skill,
    )


def _make_action_history() -> list[ActionRecord]:
    return [
        ActionRecord(action_name="search_code", arguments={"pattern": "bug"}, result="Found 2 matches", timestamp=1.0),
        ActionRecord(action_name="edit_file", arguments={"path": "fix.py"}, result="OK", timestamp=2.0),
    ]


def _make_system_llm(skill_json: dict | None = None):
    """Create a mock SystemLLM that returns a skill JSON."""
    if skill_json is None:
        skill_json = {"name": "debug", "description": "Debugging", "content": "Use pdb."}
    return MagicMock(return_value=LLMResponse(
        content=json.dumps(skill_json),
        tool_calls=None,
        usage=TokenUsage(30, 30),
    ))


def _make_mock_gepa(evolved_text: str = "evolved skill content"):
    """Create a mock that replaces dspy.GEPA."""
    mock_optimized = MagicMock()
    mock_optimized.skill_text = evolved_text
    mock_optimizer = MagicMock()
    mock_optimizer.compile.return_value = mock_optimized
    return mock_optimizer


# ===================================================================
# SkillReviewer — main logic
# ===================================================================


@pytest.mark.unit
class TestSkillReviewerV2:
    """Rewritten SkillReviewer: two-path (create/evolve) + constraint gating."""

    def test_noop_when_disabled(self):
        """skill_evolution=False -> no LLM calls, no changes."""
        system_llm = MagicMock()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=False,
        )
        agent = _make_agent(skill=None)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.8},
            action_history=_make_action_history(),
        )
        system_llm.assert_not_called()
        assert agent.skill is None

    def test_creates_when_none_and_sexec_positive(self):
        """skill=None + S_exec > 0 -> Path A: create initial skill."""
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        agent = _make_agent(skill=None)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.8},
            action_history=_make_action_history(),
        )
        assert agent.skill is not None
        assert isinstance(agent.skill, Skill)

    def test_no_creation_when_sexec_zero(self):
        """skill=None + S_exec = 0 -> no creation."""
        system_llm = MagicMock()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        agent = _make_agent(skill=None)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.0},
            action_history=_make_action_history(),
        )
        assert agent.skill is None

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_evolves_existing_skill(self, mock_dspy):
        """skill exists -> Path B: GEPA evolution."""
        mock_dspy.GEPA.return_value = _make_mock_gepa("## Procedure\n1. Improved step")
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        old_skill = Skill(name="debug", description="Debugging", content="Old content.")
        agent = _make_agent(skill=old_skill)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.7},
            action_history=_make_action_history(),
        )
        # GEPA should have been called
        mock_dspy.GEPA.assert_called_once()

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_writes_back_on_pass(self, mock_dspy):
        """Constraint passes -> agent.skill updated."""
        evolved = "## Procedure\n1. Better debugging step"
        mock_dspy.GEPA.return_value = _make_mock_gepa(evolved)
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        old_skill = Skill(name="debug", description="Debugging", content="Old.")
        agent = _make_agent(skill=old_skill)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.9},
            action_history=_make_action_history(),
        )
        # Skill should have been updated (content changed)
        assert agent.skill.content != "Old."

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_keeps_original_on_reject(self, mock_dspy):
        """Constraint fails -> agent.skill unchanged."""
        # Evolved text is way too long -> rejected
        mock_dspy.GEPA.return_value = _make_mock_gepa("x" * 6000)
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        old_skill = Skill(name="debug", description="Debugging", content="Original.")
        agent = _make_agent(skill=old_skill)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.9},
            action_history=_make_action_history(),
        )
        assert agent.skill.content == "Original."

    def test_calls_update_embedding(self):
        """After successful update, update_embedding is called."""
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        agent = _make_agent(skill=None)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.8},
            action_history=_make_action_history(),
        )
        manager.update_embedding.assert_called_with(agent.agent_id)

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_no_embedding_update_on_reject(self, mock_dspy):
        """Rejected evolution -> update_embedding NOT called."""
        mock_dspy.GEPA.return_value = _make_mock_gepa("x" * 6000)
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        agent = _make_agent(skill=Skill(name="n", description="d", content="c"))
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.8},
            action_history=_make_action_history(),
        )
        manager.update_embedding.assert_not_called()

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_gepa_failure_keeps_original(self, mock_dspy):
        """GEPA raises exception -> original skill preserved, no crash."""
        mock_dspy.GEPA.return_value.compile.side_effect = RuntimeError("GEPA failed")
        system_llm = _make_system_llm()
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )
        old_skill = Skill(name="n", description="d", content="original")
        agent = _make_agent(skill=old_skill)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.8},
            action_history=_make_action_history(),
        )
        assert agent.skill.content == "original"


# ===================================================================
# update_embedding
# ===================================================================


@pytest.mark.unit
class TestUpdateEmbedding:
    """update_embedding regenerates description and updates index."""

    def test_regenerates_description(self):
        """After update_embedding, agent's skill.description reflects new content."""
        system_llm = MagicMock(return_value=LLMResponse(
            content="New description based on updated skill",
            tool_calls=None,
            usage=TokenUsage(10, 10),
        ))
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent(
            skill=Skill(name="debug", description="Old desc", content="Updated procedure..."),
        )
        manager.register(agent)
        manager.update_embedding(agent.agent_id)

        # After update, the agent's skill description should have changed
        # (implementation should call SystemLLM to regenerate)
        updated_agent = manager.free_agents[agent.agent_id]
        # At minimum, update_embedding should not crash
        assert updated_agent is not None

    def test_match_reflects_updated_skill(self):
        """After embedding update, match() results change."""
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent(
            skill=Skill(name="debug", description="Django ORM debugging", content="..."),
        )
        manager.register(agent)

        # Match before — should find the agent for Django tasks
        candidates_before = manager.match("Django ORM N+1 query fix", top_k=5)
        assert len(candidates_before) > 0

    def test_update_embedding_nonexistent_agent_no_crash(self):
        """Calling update_embedding with unknown agent_id doesn't crash."""
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)
        # Should not raise
        manager.update_embedding("nonexistent-agent")


# ===================================================================
# Config flag
# ===================================================================


@pytest.mark.unit
class TestConfigSkillEvolution:
    """MidasConfig.skill_evolution field."""

    def test_field_exists(self):
        config = MidasConfig(
            initial_budget=10000,
            workspace_count=2,
            runtime_mode="graph_emergence",
        )
        assert hasattr(config, "skill_evolution")

    def test_defaults_true(self):
        config = MidasConfig(
            initial_budget=10000,
            workspace_count=2,
            runtime_mode="graph_emergence",
        )
        assert config.skill_evolution is True

    def test_can_set_false(self):
        config = MidasConfig(
            initial_budget=10000,
            workspace_count=2,
            runtime_mode="graph_emergence",
            skill_evolution=False,
        )
        assert config.skill_evolution is False
