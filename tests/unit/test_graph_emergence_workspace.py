"""Unit tests for GraphEmergenceWorkspace."""
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.skill import SkillReviewer
from midas_agent.types import Issue
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


@pytest.mark.unit
class TestGraphEmergenceWorkspace:
    """Tests for the GraphEmergenceWorkspace class."""

    def _make_call_llm(self):
        """Create a fake call_llm callback."""
        return MagicMock(
            return_value=LLMResponse(
                content="response",
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        )

    def _make_agent(self) -> Agent:
        """Create a test responsible Agent."""
        soul = Soul(system_prompt="You are the workspace lead.")
        return Agent(agent_id="lead-1", soul=soul, agent_type="workspace_bound")

    def _make_workspace(self) -> GraphEmergenceWorkspace:
        """Create a GraphEmergenceWorkspace with mocked dependencies."""
        return GraphEmergenceWorkspace(
            workspace_id="ws-ge-1",
            responsible_agent=self._make_agent(),
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            free_agent_manager=MagicMock(spec=FreeAgentManager),
            skill_reviewer=MagicMock(spec=SkillReviewer),
        )

    def test_is_workspace_subclass(self):
        """GraphEmergenceWorkspace is a subclass of Workspace."""
        assert issubclass(GraphEmergenceWorkspace, Workspace)

    def test_construction(self):
        """GraphEmergenceWorkspace can be constructed with all required arguments."""
        ws = self._make_workspace()

        assert ws is not None

    def test_receive_budget(self):
        """receive_budget() accepts a token budget amount."""
        ws = self._make_workspace()

        ws.receive_budget(1000)  # Should not raise

    def test_execute_starts_plan_execute(self):
        """execute() starts the PlanExecuteAgent to handle the issue."""
        ws = self._make_workspace()
        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Implement feature X",
        )

        ws.execute(issue)  # Should not raise; internally starts PlanExecuteAgent

    def test_submit_patch(self):
        """submit_patch() persists the generated patch."""
        ws = self._make_workspace()

        ws.submit_patch()  # Should not raise

    def test_post_episode_returns_none(self):
        """post_episode() always returns None for GraphEmergenceWorkspace (no config evolution)."""
        ws = self._make_workspace()

        result = ws.post_episode({"score": 0.8, "cost": 300})

        assert result is None
