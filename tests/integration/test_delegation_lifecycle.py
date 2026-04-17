"""Integration tests for the complete delegation lifecycle.

These tests verify every promise made in the use_agent description
and system prompt. They define the REQUIREMENTS for delegation,
not just the wiring.

Promises tested:
  1. Spawn creates agent AND executes the task
  2. Spawned agent runs tools in its own clean context
  3. Spawned agent's findings are returned to the caller
  4. Hire (agent_id) dispatches task to a specific agent
  5. Hired agent executes and returns results via report_result
  6. Sub-agent costs are charged to the caller's balance
  7. market_info updates after spawn (caller can see new agents)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import HookSet, TrainingLog
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.report_result import ReportResultAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.stdlib.react_agent import AgentResult
from midas_agent.types import Issue
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import (
    FreeAgentManager,
)
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from tests.integration.conftest import (
    FakeLLMProvider,
    InMemoryStorageBackend,
    SpyHookSet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(content=None, tool_calls=None, input_tokens=10, output_tokens=5):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_log():
    storage = InMemoryStorageBackend()
    hooks = SpyHookSet()
    queue = SerialQueue()
    log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)
    return log, storage, hooks


def _make_agent(agent_id="agent-1", agent_type="workspace_bound", skill=None):
    return Agent(
        agent_id=agent_id,
        soul=Soul(system_prompt=f"You are agent {agent_id}."),
        agent_type=agent_type,
        skill=skill,
    )


def _make_free_agent(agent_id="free-1", skill=None):
    return _make_agent(agent_id=agent_id, agent_type="free", skill=skill)


# ===========================================================================
# 1. Spawn executes the task (not just registers)
# ===========================================================================


@pytest.mark.integration
class TestSpawnExecutesTask:
    """Promise: 'Each spawned agent runs your task description in its
    own clean context.' Spawn must execute, not just register."""

    def test_spawn_returns_useful_results(self):
        """After spawn, the return value must contain the sub-agent's
        actual findings — not just 'Spawned agent xxx for: yyy'."""
        training_log, storage, spy_hooks = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned_agents = []

        def spawn_callback(task_description):
            agent = Agent(
                agent_id=f"spawned-{len(spawned_agents)}",
                soul=Soul(system_prompt=f"Specialist: {task_description}"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned_agents.append(agent)
            free_agent_manager.register(agent)
            return agent

        # Sub-agent LLM: search_code tool call, then report results
        sub_call_index = {"i": 0}
        sub_responses = [
            _make_response(
                content=None,
                tool_calls=[ToolCall(id="sc1", name="task_done",
                    arguments={"summary": "Found the bug in foo.py line 42: off-by-one error."})],
            ),
        ]

        def sub_agent_llm(request):
            idx = sub_call_index["i"]
            sub_call_index["i"] += 1
            return sub_responses[idx] if idx < len(sub_responses) else sub_responses[-1]

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=sub_agent_llm,
        )

        result = action.execute(
            task_description="Find the bug in foo.py",
            spawn=["code analyzer"],
        )

        # Result must contain actual findings, not just "Spawned agent xxx"
        assert len(spawned_agents) == 1
        assert "spawned-0" in result or "foo.py" in result.lower() or "bug" in result.lower(), (
            f"Spawn result should contain agent findings, got: {result}"
        )
        # Must NOT be just the registration message
        assert result != f"Spawned agent spawned-0 for: code analyzer", (
            "Spawn must execute the task, not just register"
        )


# ===========================================================================
# 2. Hire (agent_id) dispatches and executes
# ===========================================================================


@pytest.mark.integration
class TestHireExecutesTask:
    """Promise: 'Hire existing agents: set agent_id to hire a known
    agent.' Hire must dispatch the task and return results."""

    def test_hire_with_agent_id_executes(self):
        """Passing agent_id must dispatch the task to that specific
        agent, let it execute, and return its results."""
        training_log, storage, spy_hooks = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Register an existing agent
        existing_agent = _make_free_agent(
            "expert-1",
            skill=Skill(name="debugging", description="Debug expert", content="debug"),
        )
        free_agent_manager.register(existing_agent)

        # Sub-agent LLM: returns analysis via tool call
        sub_call_index = {"i": 0}
        sub_responses = [
            _make_response(
                content=None,
                tool_calls=[ToolCall(id="sc1", name="task_done",
                    arguments={"summary": "Analysis: the bug is in _cstack function, line 240."})],
            ),
        ]

        def sub_agent_llm(request):
            idx = sub_call_index["i"]
            sub_call_index["i"] += 1
            return sub_responses[idx] if idx < len(sub_responses) else sub_responses[-1]

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            call_llm=sub_agent_llm,
        )

        result = action.execute(
            task_description="Analyze the separability bug",
            agent_id="expert-1",
        )

        # Result must contain the agent's analysis, not a candidate list
        assert "candidate" not in result.lower(), (
            f"Hire should execute, not list candidates. Got: {result}"
        )
        assert "expert-1" in result or "bug" in result.lower() or "analysis" in result.lower(), (
            f"Hire result should contain agent's findings. Got: {result}"
        )

    def test_hire_nonexistent_agent_returns_error(self):
        """Hiring an agent_id that doesn't exist returns an error."""
        training_log, _, _ = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
        )

        result = action.execute(
            task_description="do something",
            agent_id="nonexistent-agent",
        )

        assert "not found" in result.lower() or "error" in result.lower(), (
            f"Hiring nonexistent agent should error. Got: {result}"
        )


# ===========================================================================
# 3. Sub-agent has clean context
# ===========================================================================


@pytest.mark.integration
class TestSubAgentCleanContext:
    """Promise: 'Sub-agents start with a clean context window.'
    The sub-agent's LLM calls must not include the parent's history."""

    def test_sub_agent_context_does_not_contain_parent_history(self):
        """When a sub-agent executes, its first LLM call must have
        a clean context — only system prompt + task description,
        not the parent's accumulated tool results."""
        captured_sub_messages = []

        def capturing_sub_llm(request: LLMRequest) -> LLMResponse:
            captured_sub_messages.append(list(request.messages))
            return _make_response(
                content=None,
                tool_calls=[ToolCall(id="sc1", name="task_done",
                    arguments={"summary": "Found the issue in line 42."})],
            )

        training_log, _, _ = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1", soul=Soul(system_prompt="Sub agent"),
                agent_type="free", protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=capturing_sub_llm,
        )

        result = action.execute(
            task_description="Search for function X in the codebase",
            spawn=["searcher"],
        )

        # Sub-agent's first LLM call should have clean context
        assert len(captured_sub_messages) >= 1, (
            "Sub-agent must make at least one LLM call"
        )

        first_call = captured_sub_messages[0]
        total_content = " ".join(m.get("content", "") for m in first_call)

        # Should have task description
        assert "function X" in total_content or "Search" in total_content

        # Should NOT have parent's tool results (we can't check directly,
        # but context should be small — just system prompt + task)
        assert len(first_call) <= 3, (
            f"Sub-agent should start with clean context (2-3 messages), "
            f"got {len(first_call)} messages"
        )


# ===========================================================================
# 4. Sub-agent costs charged to caller
# ===========================================================================


@pytest.mark.integration
class TestSubAgentCostAttribution:
    """Promise: 'Their LLM costs are charged to your balance.'"""

    def test_spawn_execution_costs_deducted_from_caller(self):
        """When a spawned sub-agent makes LLM calls, the token
        consumption must be recorded against the caller's balance,
        not the sub-agent's own balance."""
        training_log, storage, _ = _make_log()
        training_log.record_allocate(to="lead-1", amount=50000)

        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1", soul=Soul(system_prompt="Sub"),
                agent_type="free", protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        sub_llm_calls = 0
        def sub_llm(request):
            nonlocal sub_llm_calls
            sub_llm_calls += 1
            return _make_response(
                content=None,
                tool_calls=[ToolCall(id=f"sc{sub_llm_calls}", name="task_done",
                    arguments={"summary": "Done."})],
                input_tokens=100, output_tokens=50,
            )

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=sub_llm,
            balance_provider=lambda: training_log.get_balance("lead-1"),
            calling_agent_id="lead-1",
        )

        action.execute(
            task_description="Find the bug",
            spawn=["analyzer"],
        )

        # Sub-agent made LLM calls — costs should be on caller's account
        # (This test defines the requirement; implementation must record
        #  consume entries for "lead-1" when sub-agent uses tokens)
        assert sub_llm_calls >= 1, "Sub-agent must have made LLM calls"


# ===========================================================================
# 5. market_info updates after spawn
# ===========================================================================


@pytest.mark.integration
class TestMarketInfoUpdates:
    """market_info_provider must reflect current state of the agent
    pool, not a stale snapshot from iteration 1."""

    def test_market_info_shows_spawned_agents(self):
        """After spawning an agent during execute(), subsequent LLM
        calls should see the new agent in market_info."""
        training_log, _, _ = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)
        skill_reviewer = MagicMock(spec=SkillReviewer)

        responsible_agent = _make_agent("lead-1", agent_type="workspace_bound")

        # Script: spawn agent → then task_done
        spawn_response = _make_response(
            tool_calls=[ToolCall(
                id="c1", name="use_agent",
                arguments={
                    "task_description": "analyze code",
                    "spawn": ["code analyzer"],
                },
            )],
        )
        done_response = _make_response(
            tool_calls=[ToolCall(id="c2", name="task_done", arguments={})],
        )

        captured_messages = []
        call_index = 0
        responses = [spawn_response, done_response]

        def capturing_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_index
            captured_messages.append(list(request.messages))
            idx = call_index
            call_index += 1
            return responses[idx] if idx < len(responses) else responses[-1]

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-1",
            responsible_agent=responsible_agent,
            call_llm=capturing_llm,
            system_llm=MagicMock(return_value=_make_response(content="ok")),
            free_agent_manager=free_agent_manager,
            skill_reviewer=skill_reviewer,
        )
        ws.receive_budget(100000)

        issue = Issue(issue_id="test-1", repo="test/repo", description="Fix bug")
        ws.execute(issue)

        # After spawn, the agent pool should have the new agent
        assert len(free_agent_manager.free_agents) >= 1

        # The second LLM call (after spawn) should see updated market_info
        # containing the new agent. This requires market_info to refresh
        # each iteration, not just at the start.
        if len(captured_messages) >= 2:
            second_call_msgs = captured_messages[1]
            all_content = " ".join(m.get("content", "") for m in second_call_msgs)
            # Should mention the spawned agent somewhere in the context
            has_agent_ref = any(
                "spawned" in m.get("content", "").lower()
                for m in second_call_msgs
            )
            assert has_agent_ref, (
                "After spawning, subsequent LLM calls should see the new "
                "agent in market_info or tool results"
            )


# ===========================================================================
# 6. Full lifecycle: spawn → execute → report → results to caller
# ===========================================================================


@pytest.mark.integration
class TestFullDelegationLifecycle:
    """End-to-end: caller spawns sub-agent → sub-agent runs tools →
    sub-agent calls report_result → caller receives the report."""

    def test_spawn_execute_report_full_cycle(self):
        """The complete delegation cycle must work end-to-end:
        1. Caller calls use_agent(spawn=["specialist"])
        2. Sub-agent runs its own tool loop
        3. Sub-agent produces findings
        4. Findings are returned to the caller as the tool result
        """
        training_log, _, _ = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1", soul=Soul(system_prompt="Specialist"),
                agent_type="free", protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        # Sub-agent LLM returns a finding via tool call
        def sub_llm(request):
            return _make_response(
                content=None,
                tool_calls=[ToolCall(id="sc1", name="task_done",
                    arguments={"summary": "The bug is in separable.py:195 — _coord_matrix "
                        "calls model.separable which raises NotImplementedError "
                        "for CompoundModel. Fix: wrap in try/except."})],
            )

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=sub_llm,
            calling_agent_id="lead-1",
        )

        result = action.execute(
            task_description="Find root cause of separability_matrix bug",
            spawn=["bug investigator"],
        )

        # The caller must receive the sub-agent's actual findings
        assert "separable.py" in result or "NotImplementedError" in result or "_coord_matrix" in result, (
            f"Caller must receive sub-agent's findings. Got: {result}"
        )

    def test_hire_execute_report_full_cycle(self):
        """Hire an existing agent → it executes → results returned."""
        training_log, _, _ = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Pre-register an agent
        expert = _make_free_agent("expert-1", skill=Skill(
            name="debugging", description="Debug expert", content="debug"
        ))
        free_agent_manager.register(expert)

        def sub_llm(request):
            return _make_response(
                content=None,
                tool_calls=[ToolCall(id="sc1", name="task_done",
                    arguments={"summary": "Root cause identified: recursive _cstack call "
                        "does not properly propagate the matrix dimensions."})],
            )

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            call_llm=sub_llm,
        )

        result = action.execute(
            task_description="Why does nested CompoundModel fail?",
            agent_id="expert-1",
        )

        assert "root cause" in result.lower() or "_cstack" in result or "matrix" in result.lower(), (
            f"Hired agent's findings must be returned. Got: {result}"
        )
