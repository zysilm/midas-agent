"""Unit tests for Relationship data class."""
import pytest

from midas_agent.workspace.graph_emergence.relationship import Relationship


@pytest.mark.unit
class TestRelationship:
    """Tests for the Relationship data class."""

    def test_relationship_protection(self):
        """Relationship of type 'protection' stores all fields correctly."""
        rel = Relationship(
            type="protection",
            from_agent_id="manager-1",
            to_agent_id="worker-1",
            workspace_id="ws-1",
            status="active",
        )

        assert rel.type == "protection"
        assert rel.from_agent_id == "manager-1"
        assert rel.to_agent_id == "worker-1"
        assert rel.workspace_id == "ws-1"
        assert rel.status == "active"

    def test_relationship_hire(self):
        """Relationship of type 'hire' stores all fields correctly."""
        rel = Relationship(
            type="hire",
            from_agent_id="employer-1",
            to_agent_id="freelancer-1",
            workspace_id="ws-2",
            status="active",
        )

        assert rel.type == "hire"
        assert rel.from_agent_id == "employer-1"
        assert rel.to_agent_id == "freelancer-1"
        assert rel.workspace_id == "ws-2"
        assert rel.status == "active"

    def test_relationship_status_transitions(self):
        """Relationship status can be 'active', 'completed', or 'terminated'."""
        rel = Relationship(
            type="hire",
            from_agent_id="a1",
            to_agent_id="a2",
            workspace_id="ws-1",
            status="active",
        )
        assert rel.status == "active"

        rel.status = "completed"
        assert rel.status == "completed"

        rel.status = "terminated"
        assert rel.status == "terminated"
