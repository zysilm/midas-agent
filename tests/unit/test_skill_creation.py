"""Unit tests for skill creation via SkillReviewer and create_initial_skill.

Tests verify improved prompt content, edge cases, and correct skill lifecycle.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.skill_evolution import create_initial_skill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(skill: Skill | None = None) -> Agent:
    return Agent(
        agent_id="fa-1",
        soul=Soul(system_prompt="You are a debugging agent."),
        agent_type="free",
        skill=skill,
    )


def _make_action_history() -> list[ActionRecord]:
    return [
        ActionRecord(
            action_name="search_code",
            arguments={"pattern": "TypeError"},
            result="Found 2 matches in calculator.py",
            timestamp=1.0,
        ),
        ActionRecord(
            action_name="read_file",
            arguments={"path": "calculator.py"},
            result="class Calculator:\n    def divide(self, a, b):\n        return a / b",
            timestamp=2.0,
        ),
        ActionRecord(
            action_name="edit_file",
            arguments={"path": "calculator.py"},
            result="OK",
            timestamp=3.0,
        ),
    ]


def _make_system_llm(skill_json: dict | None = None):
    if skill_json is None:
        skill_json = {
            "name": "debug_computation_errors",
            "description": "Debug and fix computation errors in Python code",
            "content": "## Procedure\n1. Search for error patterns\n2. Read context\n3. Fix",
        }

    def fake(request):
        return LLMResponse(
            content=json.dumps(skill_json),
            tool_calls=None,
            usage=TokenUsage(input_tokens=50, output_tokens=100),
        )

    return fake


# ===========================================================================
# 6. Skill created on success
# ===========================================================================


@pytest.mark.unit
class TestSkillCreatedOnSuccess:
    """Mock system_llm returns valid skill JSON. s_exec=1.0.
    Verify agent.skill is set with correct name/description/content."""

    def test_skill_created_on_success(self):
        system_llm = _make_system_llm({
            "name": "debug_computation_errors",
            "description": "Debug and fix computation errors",
            "content": "Step 1: search. Step 2: fix.",
        })
        manager = MagicMock(spec=FreeAgentManager)
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=manager,
            skill_evolution=True,
        )

        agent = _make_agent(skill=None)
        reviewer.review(
            agent=agent,
            eval_results={"s_exec": 1.0},
            action_history=_make_action_history(),
        )

        assert agent.skill is not None
        assert agent.skill.name == "debug_computation_errors"
        assert agent.skill.description == "Debug and fix computation errors"
        assert "search" in agent.skill.content.lower()


# ===========================================================================
# 7. Skill NOT created on failure
# ===========================================================================


@pytest.mark.unit
class TestSkillNotCreatedOnFailure:
    """s_exec=0. Verify agent.skill stays None."""

    def test_no_skill_on_zero_score(self):
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
        system_llm.assert_not_called()


# ===========================================================================
# 8. Improved prompt has pattern guidance
# ===========================================================================


@pytest.mark.unit
class TestImprovedPromptGuidance:
    """Mock system_llm, capture the prompt it receives.
    Verify it contains pattern-level extraction guidance."""

    def test_prompt_contains_pattern_guidance(self):
        captured_requests = []

        def capturing_llm(request):
            captured_requests.append(request)
            return LLMResponse(
                content=json.dumps({
                    "name": "test",
                    "description": "test",
                    "content": "test",
                }),
                tool_calls=None,
                usage=TokenUsage(input_tokens=50, output_tokens=100),
            )

        skill = create_initial_skill(
            system_llm=capturing_llm,
            action_history=_make_action_history(),
            eval_results={"s_exec": 0.9},
        )

        assert len(captured_requests) == 1
        user_msg = captured_requests[0].messages[1]["content"]
        assert "PATTERNS, not specific fixes" in user_msg
        assert "Do NOT mention specific line numbers" in user_msg


# ===========================================================================
# 9. Invalid JSON graceful fallback
# ===========================================================================


@pytest.mark.unit
class TestInvalidJsonFallback:
    """Mock system_llm returns garbage. Verify no crash, skill stays None."""

    def test_invalid_json_returns_none(self):
        def garbage_llm(request):
            return LLMResponse(
                content="This is not valid JSON at all!!!",
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=10),
            )

        result = create_initial_skill(
            system_llm=garbage_llm,
            action_history=_make_action_history(),
            eval_results={"s_exec": 0.8},
        )

        assert result is None


# ===========================================================================
# 10. Empty history returns None
# ===========================================================================


@pytest.mark.unit
class TestEmptyHistoryReturnsNone:
    """create_initial_skill with empty action_history returns None."""

    def test_empty_history(self):
        system_llm = MagicMock()
        result = create_initial_skill(
            system_llm=system_llm,
            action_history=[],
            eval_results={"s_exec": 0.9},
        )

        assert result is None
        system_llm.assert_not_called()


# ===========================================================================
# 11. Skill content truncated at 5000
# ===========================================================================


@pytest.mark.unit
class TestSkillContentTruncation:
    """Mock system_llm returns 10000 char content. Verify truncated to 5000."""

    def test_content_truncated(self):
        long_content = "x" * 10000

        def long_llm(request):
            return LLMResponse(
                content=json.dumps({
                    "name": "verbose_skill",
                    "description": "A skill that is too long",
                    "content": long_content,
                }),
                tool_calls=None,
                usage=TokenUsage(input_tokens=50, output_tokens=200),
            )

        result = create_initial_skill(
            system_llm=long_llm,
            action_history=_make_action_history(),
            eval_results={"s_exec": 0.9},
        )

        assert result is not None
        assert len(result.content) == 5000
