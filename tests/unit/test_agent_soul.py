"""Unit tests for Agent and Soul data classes."""
import pytest

from midas_agent.workspace.graph_emergence.agent import Agent, Soul


@pytest.mark.unit
class TestSoul:
    """Tests for the Soul data class."""

    def test_soul_fields(self):
        """Soul stores the system_prompt correctly."""
        soul = Soul(system_prompt="You are a helpful coding assistant.")

        assert soul.system_prompt == "You are a helpful coding assistant."


@pytest.mark.unit
class TestAgent:
    """Tests for the Agent data class."""

    def test_agent_fields(self):
        """Agent stores agent_id, soul, and agent_type correctly."""
        soul = Soul(system_prompt="You are an expert debugger.")
        agent = Agent(agent_id="agent-1", soul=soul, agent_type="workspace_bound")

        assert agent.agent_id == "agent-1"
        assert agent.soul is soul
        assert agent.soul.system_prompt == "You are an expert debugger."
        assert agent.agent_type == "workspace_bound"

    def test_agent_workspace_bound_type(self):
        """Agent with agent_type='workspace_bound' is correctly typed."""
        soul = Soul(system_prompt="Bound agent")
        agent = Agent(agent_id="wb-1", soul=soul, agent_type="workspace_bound")

        assert agent.agent_type == "workspace_bound"

    def test_agent_free_type(self):
        """Agent with agent_type='free' is correctly typed."""
        soul = Soul(system_prompt="Free agent")
        agent = Agent(agent_id="free-1", soul=soul, agent_type="free")

        assert agent.agent_type == "free"

    def test_agent_defaults(self):
        """Agent defaults: skill=None, protected_by=None, protecting=[]."""
        soul = Soul(system_prompt="Default agent")
        agent = Agent(agent_id="d-1", soul=soul, agent_type="free")

        assert agent.skill is None
        assert agent.protected_by is None
        assert agent.protecting == []

    def test_agent_with_protection(self):
        """Agent can have protected_by and protecting set explicitly."""
        soul = Soul(system_prompt="Protected agent")
        agent = Agent(
            agent_id="p-1",
            soul=soul,
            agent_type="workspace_bound",
            protected_by="manager-1",
            protecting=["sub-1", "sub-2"],
        )

        assert agent.protected_by == "manager-1"
        assert agent.protecting == ["sub-1", "sub-2"]
        assert len(agent.protecting) == 2
