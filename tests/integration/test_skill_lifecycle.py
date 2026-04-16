"""Integration tests for skill lifecycle: creation, evolution, injection, matching.

Tests exercise multi-component interactions across the skill evolution pipeline.
Tests are expected to FAIL until skill evolution is implemented.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.llm.types import LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer


# ===================================================================
# Helpers
# ===================================================================


def _make_agent(agent_id: str = "fa-1", skill: Skill | None = None) -> Agent:
    return Agent(
        agent_id=agent_id,
        soul=Soul(system_prompt=f"You are agent {agent_id}."),
        agent_type="free",
        skill=skill,
    )


def _make_action_history(actions: list[tuple[str, str]] | None = None) -> list[ActionRecord]:
    if actions is None:
        actions = [
            ("search_code", "Found 3 matches"),
            ("read_file", "class Calculator: ..."),
            ("edit_file", "OK"),
            ("bash", "Tests passed"),
        ]
    return [
        ActionRecord(action_name=name, arguments={}, result=result, timestamp=float(i))
        for i, (name, result) in enumerate(actions)
    ]


def _skill_json(name: str = "debug", desc: str = "Debugging", content: str = "Use pdb.") -> str:
    return json.dumps({"name": name, "description": desc, "content": content})


def _make_system_llm(responses: list[str] | None = None):
    if responses is None:
        responses = [_skill_json()]
    idx = {"i": 0}
    def fake(request):
        r = responses[min(idx["i"], len(responses) - 1)]
        idx["i"] += 1
        return LLMResponse(content=r, tool_calls=None, usage=TokenUsage(30, 30))
    return fake


def _mock_gepa(evolved_content: str):
    mock_optimized = MagicMock()
    mock_optimized.skill_text = evolved_content
    mock_optimizer = MagicMock()
    mock_optimizer.compile.return_value = mock_optimized
    return mock_optimizer


# ===================================================================
# Skill creation flow
# ===================================================================


@pytest.mark.integration
class TestSkillCreationFlow:
    """spawn agent (skill=None) -> execute episode -> post_episode -> skill created."""

    def test_spawn_then_review_creates_skill(self):
        """New agent with skill=None gets a skill after positive review."""
        system_llm = _make_system_llm([_skill_json("code-nav", "Code navigation", "Use grep.")])
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )

        agent = _make_agent("fa-new", skill=None)
        manager.register(agent)

        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 0.7},
            action_history=_make_action_history(),
        )

        assert agent.skill is not None
        assert agent.skill.name == "code-nav"

    def test_created_skill_passes_constraints(self):
        """Newly created skill is within 5000 char limit."""
        content = "## Procedure\n1. Search\n2. Read\n3. Edit"
        system_llm = _make_system_llm([_skill_json("s", "d", content)])
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )

        agent = _make_agent(skill=None)
        manager.register(agent)
        reviewer.review(agent=agent, eval_results={"s_exec": 0.6}, action_history=_make_action_history())

        assert len(agent.skill.content) <= 5000

    def test_creation_updates_embedding(self):
        """After skill creation, FreeAgentManager can match the agent."""
        system_llm = _make_system_llm([_skill_json("django-fix", "Fix Django bugs", "Check ORM...")])
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )

        agent = _make_agent("django-agent", skill=None)
        manager.register(agent)
        reviewer.review(agent=agent, eval_results={"s_exec": 0.8}, action_history=_make_action_history())

        # Agent should now be matchable
        candidates = manager.match("Django ORM bug", top_k=5)
        agent_ids = [c.agent.agent_id for c in candidates]
        assert "django-agent" in agent_ids

    def test_creation_only_on_positive_score(self):
        """S_exec=0 -> no skill creation, remains None."""
        system_llm = MagicMock()
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )

        agent = _make_agent(skill=None)
        manager.register(agent)
        reviewer.review(agent=agent, eval_results={"s_exec": 0.0}, action_history=_make_action_history())

        assert agent.skill is None


# ===================================================================
# Skill evolution flow
# ===================================================================


@pytest.mark.integration
class TestSkillEvolutionFlow:
    """Existing skill -> GEPA evolution -> constraint gate -> write back or reject."""

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_skill_evolves_after_second_episode(self, mock_dspy):
        """Episode 1: create. Episode 2: evolve. Content changes."""
        # Episode 1: creation
        system_llm = _make_system_llm([_skill_json("s", "d", "Original content.")])
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)
        reviewer = SkillReviewer(
            system_llm=system_llm, free_agent_manager=manager, skill_evolution=True,
        )

        agent = _make_agent(skill=None)
        manager.register(agent)
        reviewer.review(agent=agent, eval_results={"s_exec": 0.6}, action_history=_make_action_history())
        assert agent.skill is not None
        original_content = agent.skill.content

        # Episode 2: evolution via GEPA
        evolved_text = "## Procedure\n1. Improved search\n2. Better edit"
        mock_dspy.GEPA.return_value = _mock_gepa(evolved_text)
        reviewer.review(agent=agent, eval_results={"s_exec": 0.8}, action_history=_make_action_history())

        assert agent.skill.content != original_content

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_rejected_evolution_preserves_original(self, mock_dspy):
        """GEPA produces oversized result -> original skill kept."""
        system_llm = _make_system_llm()
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)
        reviewer = SkillReviewer(
            system_llm=system_llm, free_agent_manager=manager, skill_evolution=True,
        )

        original = Skill(name="n", description="d", content="Original.")
        agent = _make_agent(skill=original)
        manager.register(agent)

        mock_dspy.GEPA.return_value = _mock_gepa("x" * 6000)
        reviewer.review(agent=agent, eval_results={"s_exec": 0.9}, action_history=_make_action_history())

        assert agent.skill.content == "Original."

    @patch("midas_agent.workspace.graph_emergence.skill.dspy")
    def test_multi_episode_accumulation(self, mock_dspy):
        """Skill content changes across 3 episodes as agent accumulates experience."""
        system_llm = _make_system_llm([_skill_json("s", "d", "V1 content")])
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)
        reviewer = SkillReviewer(
            system_llm=system_llm, free_agent_manager=manager, skill_evolution=True,
        )

        agent = _make_agent(skill=None)
        manager.register(agent)

        contents = []

        # Episode 1: creation
        reviewer.review(agent=agent, eval_results={"s_exec": 0.5}, action_history=_make_action_history())
        contents.append(agent.skill.content)

        # Episode 2: evolution
        mock_dspy.GEPA.return_value = _mock_gepa("V2 content improved")
        reviewer.review(agent=agent, eval_results={"s_exec": 0.7}, action_history=_make_action_history())
        contents.append(agent.skill.content)

        # Episode 3: further evolution
        mock_dspy.GEPA.return_value = _mock_gepa("V3 content refined")
        reviewer.review(agent=agent, eval_results={"s_exec": 0.8}, action_history=_make_action_history())
        contents.append(agent.skill.content)

        # All 3 versions should be different
        assert len(set(contents)) == 3


# ===================================================================
# Skill injection flow
# ===================================================================


@pytest.mark.integration
class TestSkillInjectionFlow:
    """When a free agent is hired, its skill.content is injected into context."""

    def test_hired_agent_receives_skill_content(self):
        """Free agent with skill -> sub-agent's context contains skill.content."""
        from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction

        skill_content = "## Procedure\n1. Always check error logs first"
        agent = _make_agent(
            skill=Skill(name="debug", description="Debugging", content=skill_content),
        )

        call_llm_calls = []

        def fake_call_llm(request):
            call_llm_calls.append(request)
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="t1", name="report_result", arguments={"result": "done"})],
                usage=TokenUsage(10, 10),
            )

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: [MagicMock(agent=agent, similarity=1.0, price=100)],
            spawn_callback=lambda desc: agent,
            balance_provider=lambda: 10000,
            calling_agent_id="resp-1",
            call_llm=fake_call_llm,
            parent_actions=[],
        )

        delegate.execute(mode="hire", agent_id="fa-1", task="Fix the bug")

        # The sub-agent's LLM call should contain the skill content
        assert len(call_llm_calls) >= 1
        all_messages = " ".join(
            m.get("content", "") for req in call_llm_calls for m in req.messages
        )
        assert "error logs first" in all_messages

    def test_agent_without_skill_still_works(self):
        """Free agent with skill=None -> hired successfully, no skill injected."""
        from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction

        agent = _make_agent(skill=None)

        def fake_call_llm(request):
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="t1", name="report_result", arguments={"result": "done"})],
                usage=TokenUsage(10, 10),
            )

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: [MagicMock(agent=agent, similarity=1.0, price=100)],
            spawn_callback=lambda desc: agent,
            balance_provider=lambda: 10000,
            calling_agent_id="resp-1",
            call_llm=fake_call_llm,
            parent_actions=[],
        )

        # Should not crash
        result = delegate.execute(mode="hire", agent_id="fa-1", task="Fix the bug")
        assert result is not None


# ===================================================================
# Skill market matching flow
# ===================================================================


@pytest.mark.integration
class TestSkillMarketFlow:
    """Updated skills improve market matching accuracy."""

    def test_evolved_description_changes_match(self):
        """After skill evolution, match() results reflect updated description."""
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100
        manager = FreeAgentManager(pricing_engine=pricing)

        agent = _make_agent(
            "search-agent",
            skill=Skill(name="search", description="Generic code search", content="..."),
        )
        manager.register(agent)

        # Match before
        candidates_before = manager.match("Django ORM optimization", top_k=5)

        # Simulate skill evolution + embedding update
        agent.skill = Skill(
            name="django-orm",
            description="Django ORM query optimization specialist",
            content="...",
        )
        manager.update_embedding(agent.agent_id)

        # Match after — should be better for Django queries
        candidates_after = manager.match("Django ORM optimization", top_k=5)

        # The agent should appear in both, but with different similarity
        ids_before = {c.agent.agent_id for c in candidates_before}
        ids_after = {c.agent.agent_id for c in candidates_after}
        assert "search-agent" in ids_before
        assert "search-agent" in ids_after
