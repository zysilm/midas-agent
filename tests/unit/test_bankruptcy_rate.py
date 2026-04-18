"""Tests for free agent service bankruptcy rate tracking and display (#15)."""
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import HookSet, TrainingLog
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.types import Issue
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall

from tests.unit.conftest import InMemoryStorageBackend


def _make_call_llm():
    """Create a scripted call_llm that returns plan -> task_done."""
    responses = [
        LLMResponse(content="Plan: fix the bug.", tool_calls=None, usage=TokenUsage(input_tokens=10, output_tokens=5)),
        LLMResponse(content=None, tool_calls=[ToolCall(id="c1", name="search_code", arguments={"pattern": "bug"})], usage=TokenUsage(input_tokens=10, output_tokens=5)),
        LLMResponse(content=None, tool_calls=[ToolCall(id="c2", name="task_done", arguments={})], usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ]
    idx = {"i": 0}

    def call_llm(request):
        i = idx["i"]
        idx["i"] += 1
        return responses[i] if i < len(responses) else responses[-1]

    return call_llm


def _make_training_log():
    """Create a real TrainingLog backed by InMemoryStorageBackend."""
    storage = InMemoryStorageBackend()
    return TrainingLog(
        storage=storage, hooks=HookSet(), serial_queue=SerialQueue(),
    )


def _make_agent(agent_id: str, skill_name: str | None = None) -> Agent:
    """Create a test Agent with optional skill."""
    skill = None
    if skill_name:
        skill = Skill(name=skill_name, description=f"{skill_name} expert", content="...")
    return Agent(
        agent_id=agent_id,
        soul=Soul(system_prompt=f"Agent {agent_id}"),
        agent_type="free",
        skill=skill,
    )


@pytest.mark.unit
class TestComputeBankruptcyRate:
    """Test computing bankruptcy rate from training log and evicted workspace IDs."""

    def test_rate_zero_when_no_workspaces_served(self):
        """An agent that never served any workspace has bankruptcy rate 0.0."""
        training_log = _make_training_log()
        evicted_ws_ids: set[str] = {"ws-0", "ws-1"}

        rate = compute_bankruptcy_rate("agent-x", training_log, evicted_ws_ids)

        assert rate == 0.0

    def test_rate_zero_when_no_served_workspaces_evicted(self):
        """Agent served workspaces but none were evicted -> rate 0.0."""
        training_log = _make_training_log()
        # Agent consumed tokens in ws-0 and ws-1
        training_log.record_allocate(to="agent-a", amount=1000)
        training_log.record_consume(entity_id="agent-a", amount=100, workspace_id="ws-0")
        training_log.record_consume(entity_id="agent-a", amount=200, workspace_id="ws-1")
        # Neither ws-0 nor ws-1 were evicted
        evicted_ws_ids: set[str] = {"ws-5", "ws-6"}

        rate = compute_bankruptcy_rate("agent-a", training_log, evicted_ws_ids)

        assert rate == 0.0

    def test_rate_half_when_half_served_workspaces_evicted(self):
        """Agent served 2 workspaces, 1 evicted -> rate 0.5."""
        training_log = _make_training_log()
        training_log.record_allocate(to="agent-a", amount=1000)
        training_log.record_consume(entity_id="agent-a", amount=100, workspace_id="ws-0")
        training_log.record_consume(entity_id="agent-a", amount=200, workspace_id="ws-1")
        # Only ws-0 was evicted
        evicted_ws_ids: set[str] = {"ws-0"}

        rate = compute_bankruptcy_rate("agent-a", training_log, evicted_ws_ids)

        assert rate == 0.5

    def test_rate_one_when_all_served_workspaces_evicted(self):
        """Agent served 2 workspaces, both evicted -> rate 1.0."""
        training_log = _make_training_log()
        training_log.record_allocate(to="agent-a", amount=1000)
        training_log.record_consume(entity_id="agent-a", amount=50, workspace_id="ws-0")
        training_log.record_consume(entity_id="agent-a", amount=50, workspace_id="ws-1")
        evicted_ws_ids: set[str] = {"ws-0", "ws-1"}

        rate = compute_bankruptcy_rate("agent-a", training_log, evicted_ws_ids)

        assert rate == 1.0

    def test_rate_updates_with_new_consume_entries(self):
        """Bankruptcy rate reflects latest consume entries, not a stale snapshot."""
        training_log = _make_training_log()
        training_log.record_allocate(to="agent-a", amount=1000)
        training_log.record_consume(entity_id="agent-a", amount=50, workspace_id="ws-0")
        evicted_ws_ids: set[str] = {"ws-0"}

        # Initially: 1 served, 1 evicted -> 1.0
        rate1 = compute_bankruptcy_rate("agent-a", training_log, evicted_ws_ids)
        assert rate1 == 1.0

        # Agent now serves ws-1 (not evicted) -> 1 of 2 evicted -> 0.5
        training_log.record_consume(entity_id="agent-a", amount=50, workspace_id="ws-1")
        rate2 = compute_bankruptcy_rate("agent-a", training_log, evicted_ws_ids)
        assert rate2 == 0.5

    def test_multiple_consumes_same_workspace_counted_once(self):
        """An agent consuming multiple times in the same workspace
        only counts that workspace once."""
        training_log = _make_training_log()
        training_log.record_allocate(to="agent-a", amount=1000)
        training_log.record_consume(entity_id="agent-a", amount=10, workspace_id="ws-0")
        training_log.record_consume(entity_id="agent-a", amount=20, workspace_id="ws-0")
        training_log.record_consume(entity_id="agent-a", amount=30, workspace_id="ws-1")
        evicted_ws_ids: set[str] = {"ws-0"}

        rate = compute_bankruptcy_rate("agent-a", training_log, evicted_ws_ids)

        # served = {ws-0, ws-1}, evicted intersection = {ws-0} -> 1/2 = 0.5
        assert rate == 0.5


@pytest.mark.unit
class TestEnvironmentContextBankruptcyDisplay:
    """Test that EnvironmentContext agent lines include bankruptcy rate."""

    def _make_workspace_with_agents(
        self,
        agents: list[Agent],
        training_log: TrainingLog,
        evicted_ws_ids: set[str] | None = None,
    ) -> GraphEmergenceWorkspace:
        """Create a GraphEmergenceWorkspace with registered free agents."""
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)
        for agent in agents:
            free_agent_manager.register(agent)

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-ge-1",
            responsible_agent=Agent(
                agent_id="lead-1",
                soul=Soul(system_prompt="You are the workspace lead."),
                agent_type="workspace_bound",
            ),
            call_llm=_make_call_llm(),
            system_llm=_make_call_llm(),
            free_agent_manager=free_agent_manager,
            skill_reviewer=MagicMock(spec=SkillReviewer),
            training_log=training_log,
            evicted_ws_ids=evicted_ws_ids or set(),
        )
        ws.receive_budget(10000)
        return ws

    def test_agent_line_includes_bankruptcy_rate(self):
        """Each agent line in env_context must include bankruptcy=X.XX."""
        training_log = _make_training_log()
        agent = _make_agent("expert-a", skill_name="debugging")

        # Agent served ws-0 (evicted) and ws-1 (not evicted) -> rate=0.50
        training_log.record_allocate(to="expert-a", amount=1000)
        training_log.record_consume(entity_id="expert-a", amount=50, workspace_id="ws-0")
        training_log.record_consume(entity_id="expert-a", amount=50, workspace_id="ws-1")
        evicted = {"ws-0"}

        ws = self._make_workspace_with_agents([agent], training_log, evicted)
        issue = Issue(issue_id="issue-1", repo="test/repo", description="Fix bug")

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        assert "expert-a: debugging (price=" in captured_xml
        assert "bankruptcy=0.50" in captured_xml

    def test_agent_line_bankruptcy_zero_when_no_history(self):
        """Agent with no consume history shows bankruptcy=0.00."""
        training_log = _make_training_log()
        agent = _make_agent("expert-b", skill_name="testing")

        ws = self._make_workspace_with_agents([agent], training_log)
        issue = Issue(issue_id="issue-1", repo="test/repo", description="Fix bug")

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        assert "bankruptcy=0.00" in captured_xml

    def test_agent_line_format(self):
        """Agent line must be: agent-id: skill (price=N, bankruptcy=X.XX)."""
        training_log = _make_training_log()
        agent = _make_agent("expert-c", skill_name="refactoring")

        ws = self._make_workspace_with_agents([agent], training_log)
        issue = Issue(issue_id="issue-1", repo="test/repo", description="Fix bug")

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        price = PricingEngine(training_log=training_log).calculate_price(agent)
        expected_fragment = f"expert-c: refactoring (price={price}, bankruptcy=0.00)"
        assert expected_fragment in captured_xml, (
            f"Expected '{expected_fragment}' in env_context_xml:\n{captured_xml}"
        )

    def test_multiple_agents_each_show_own_bankruptcy(self):
        """Multiple agents each show their own computed bankruptcy rate."""
        training_log = _make_training_log()
        agent_a = _make_agent("agent-a", skill_name="debugging")
        agent_b = _make_agent("agent-b", skill_name="testing")

        # agent-a served ws-0 (evicted) -> rate=1.00
        training_log.record_allocate(to="agent-a", amount=1000)
        training_log.record_consume(entity_id="agent-a", amount=50, workspace_id="ws-0")

        # agent-b served ws-1 (not evicted) -> rate=0.00
        training_log.record_allocate(to="agent-b", amount=1000)
        training_log.record_consume(entity_id="agent-b", amount=50, workspace_id="ws-1")

        evicted = {"ws-0"}

        ws = self._make_workspace_with_agents([agent_a, agent_b], training_log, evicted)
        issue = Issue(issue_id="issue-1", repo="test/repo", description="Fix bug")

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        assert "bankruptcy=1.00" in captured_xml  # agent-a
        assert "bankruptcy=0.00" in captured_xml  # agent-b


# Import the function under test — placed after test class definitions
# so tests fail at import time if the function doesn't exist yet (red phase).
from midas_agent.workspace.graph_emergence.free_agent_manager import compute_bankruptcy_rate
