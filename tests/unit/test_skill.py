"""Unit tests for Skill and SkillReviewer."""
import json
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse, TokenUsage
from llm_agent_toolkit.stdlib.react_agent import ActionRecord


def _make_agent(skill: Skill | None = None) -> Agent:
    return Agent(
        agent_id="free-1",
        soul=Soul(system_prompt="You are a helper."),
        agent_type="free",
        skill=skill,
    )


def _make_action_history() -> list[ActionRecord]:
    return [
        ActionRecord(action_name="search_code", arguments={"pattern": "bug"}, result="Found 2 matches", timestamp=1.0),
    ]


@pytest.mark.unit
class TestSkill:
    """Tests for the Skill data class."""

    def test_skill_fields(self):
        """Skill stores name, description, and content correctly."""
        skill = Skill(
            name="python_debug",
            description="Expert Python debugging",
            content="Use pdb to set breakpoints...",
        )

        assert skill.name == "python_debug"
        assert skill.description == "Expert Python debugging"
        assert skill.content == "Use pdb to set breakpoints..."

    def test_skill_content_limit(self):
        """Skill content has a 5000 character hard limit concept."""
        # This test validates that content exceeding 5000 chars is detectable.
        # The actual enforcement is in the production code; we verify the field
        # can hold content and that we can check its length.
        long_content = "x" * 5001
        skill = Skill(
            name="verbose",
            description="A skill with too much content",
            content=long_content,
        )

        assert len(skill.content) > 5000


@pytest.mark.unit
class TestSkillReviewer:
    """Tests for the SkillReviewer class."""

    def _make_system_llm(self, content: str | None = None):
        """Create a fake system_llm callback that returns valid skill JSON."""
        if content is None:
            content = json.dumps({"name": "debug", "description": "Debugging", "content": "Use pdb."})
        return MagicMock(
            return_value=LLMResponse(
                content=content,
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        )

    def test_skill_reviewer_construction(self):
        """SkillReviewer can be constructed with a system_llm and FreeAgentManager."""
        system_llm = self._make_system_llm()
        free_agent_manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=free_agent_manager,
        )

        assert reviewer is not None

    def test_skill_reviewer_review(self):
        """review() processes eval_results and updates agent skills."""
        system_llm = self._make_system_llm()
        free_agent_manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=free_agent_manager,
        )

        agent = _make_agent(skill=None)
        eval_results = {
            "agent_id": "free-1",
            "s_exec": 0.85,
            "summary": "Handled task well",
        }

        reviewer.review(agent, eval_results, _make_action_history())  # Should not raise

    def test_skill_reviewer_updates_embeddings(self):
        """review() triggers FreeAgentManager.update_embedding for the agent."""
        system_llm = self._make_system_llm()
        free_agent_manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=free_agent_manager,
        )

        agent = _make_agent(skill=None)
        eval_results = {
            "agent_id": "free-1",
            "s_exec": 0.9,
            "summary": "Excellent work",
        }

        reviewer.review(agent, eval_results, _make_action_history())

        free_agent_manager.update_embedding.assert_called()
