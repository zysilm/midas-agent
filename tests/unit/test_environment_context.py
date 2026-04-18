"""Tests for EnvironmentContext serialization."""
import pytest
from datetime import date


class TestEnvironmentContextSerialization:
    """EnvironmentContext serializes to XML matching Codex format."""

    def test_serialize_to_xml_all_fields(self):
        """All fields present → full XML block with all tags."""
        from midas_agent.context.environment import EnvironmentContext

        ctx = EnvironmentContext(
            cwd="/testbed",
            shell="bash",
            current_date="2026-04-18",
            balance=1000000,
            available_agents=[
                "agent-1: debugging (price=500)",
                "agent-2: testing (price=300)",
            ],
        )
        xml = ctx.serialize_to_xml()

        assert "<environment_context>" in xml
        assert "</environment_context>" in xml
        assert "<cwd>/testbed</cwd>" in xml
        assert "<shell>bash</shell>" in xml
        assert "<current_date>2026-04-18</current_date>" in xml
        assert "<balance>1000000</balance>" in xml
        assert "<available_agents>" in xml
        assert "agent-1: debugging (price=500)" in xml
        assert "agent-2: testing (price=300)" in xml
        assert "</available_agents>" in xml

    def test_serialize_to_xml_minimal(self):
        """Only cwd set → no empty tags for missing fields."""
        from midas_agent.context.environment import EnvironmentContext

        ctx = EnvironmentContext(cwd="/testbed")
        xml = ctx.serialize_to_xml()

        assert "<cwd>/testbed</cwd>" in xml
        assert "<shell>" not in xml
        assert "<current_date>" not in xml
        assert "<balance>" not in xml
        assert "<available_agents>" not in xml

    def test_serialize_to_xml_no_agents(self):
        """Empty available_agents list → tag omitted."""
        from midas_agent.context.environment import EnvironmentContext

        ctx = EnvironmentContext(
            cwd="/testbed",
            balance=500000,
            available_agents=[],
        )
        xml = ctx.serialize_to_xml()

        assert "<cwd>/testbed</cwd>" in xml
        assert "<balance>500000</balance>" in xml
        assert "<available_agents>" not in xml
