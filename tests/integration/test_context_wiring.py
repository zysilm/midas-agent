"""Integration tests for context management wiring.

Verifies that truncation and compaction flow from MidasConfig through
the full stack: config → workspace → agent → tool result truncation
and conversation compaction.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from midas_agent.config import MidasConfig
from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from llm_agent_toolkit.stdlib.actions.task_done import TaskDoneAction
from llm_agent_toolkit.stdlib.action import Action
from llm_agent_toolkit.stdlib.plan_execute_agent import PlanExecuteAgent
from llm_agent_toolkit.stdlib.react_agent import ReactAgent
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.skill import SkillReviewer
from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from llm_agent_toolkit.types import Issue

from tests.integration.conftest import (
    FakeLLMProvider,
    InMemoryStorageBackend,
    SpyHookSet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class HugeOutputAction(Action):
    """Action that returns a very large string."""

    @property
    def name(self):
        return "huge_output"

    @property
    def description(self):
        return "Returns huge output"

    @property
    def parameters(self):
        return {"query": {"type": "string", "required": True}}

    def execute(self, **kwargs):
        return "X" * 50000  # 50k chars


def _make_response(
    content=None,
    tool_calls=None,
    input_tokens=100,
    output_tokens=50,
):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# ===========================================================================
# IT: PlanExecuteAgent truncates tool output
# ===========================================================================


@pytest.mark.integration
class TestPlanExecuteAgentTruncation:
    """PlanExecuteAgent must apply truncation to tool results, same as
    ReactAgent. It has its own run() loop, so truncation must be
    implemented there too."""

    def test_plan_execute_truncates_large_tool_output(self):
        """PlanExecuteAgent with max_tool_output_chars truncates huge
        tool results before they enter the conversation messages."""
        captured_messages: list[list[dict]] = []

        call_index = 0
        responses = [
            _make_response(
                tool_calls=[ToolCall(id="c1", name="huge_output", arguments={"query": "test"})],
            ),
            _make_response(
                tool_calls=[ToolCall(id="c2", name="task_done", arguments={})],
            ),
        ]

        def capturing_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_index
            captured_messages.append(list(request.messages))
            idx = call_index
            call_index += 1
            return responses[idx] if idx < len(responses) else responses[-1]

        agent = PlanExecuteAgent(
            system_prompt="test",
            actions=[HugeOutputAction(), TaskDoneAction()],
            call_llm=capturing_llm,
            max_iterations=5,
            max_tool_output_chars=10000,
        )
        result = agent.run(context="test task")
        assert result.termination_reason == "done"

        # Second LLM call should have the tool result truncated
        assert len(captured_messages) >= 2
        second_call_msgs = captured_messages[1]
        tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1

        tool_content = tool_msgs[0]["content"]
        assert len(tool_content) < 15000, (
            f"Tool output should be truncated to ~10k but was {len(tool_content)}"
        )
        assert "characters were elided" in tool_content

    def test_plan_execute_no_truncation_when_not_set(self):
        """Without max_tool_output_chars, PlanExecuteAgent passes
        through full tool output (backward compatible)."""
        captured_messages: list[list[dict]] = []

        call_index = 0
        responses = [
            _make_response(
                tool_calls=[ToolCall(id="c1", name="huge_output", arguments={"query": "test"})],
            ),
            _make_response(
                tool_calls=[ToolCall(id="c2", name="task_done", arguments={})],
            ),
        ]

        def capturing_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_index
            captured_messages.append(list(request.messages))
            idx = call_index
            call_index += 1
            return responses[idx] if idx < len(responses) else responses[-1]

        agent = PlanExecuteAgent(
            system_prompt="test",
            actions=[HugeOutputAction(), TaskDoneAction()],
            call_llm=capturing_llm,
            max_iterations=5,
            # No max_tool_output_chars — default None
        )
        result = agent.run(context="test task")
        assert result.termination_reason == "done"

        second_call_msgs = captured_messages[1]
        tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
        tool_content = tool_msgs[0]["content"]
        # Should NOT be truncated
        assert "characters were elided" not in tool_content
        assert len(tool_content) >= 50000


# ===========================================================================
# IT: GraphEmergenceWorkspace passes truncation config to agent
# ===========================================================================


@pytest.mark.integration
class TestWorkspaceTruncationWiring:
    """GraphEmergenceWorkspace must pass max_tool_output_chars from its
    config through to the PlanExecuteAgent it creates in execute()."""

    def test_workspace_passes_truncation_to_agent(self):
        """When max_tool_output_chars is set on the workspace, tool
        results during execute() are truncated."""
        from midas_agent.scheduler.serial_queue import SerialQueue
        from midas_agent.scheduler.training_log import HookSet, TrainingLog

        training_log, _, _ = (
            TrainingLog(
                storage=InMemoryStorageBackend(),
                hooks=SpyHookSet(),
                serial_queue=SerialQueue(),
            ),
            None,
            None,
        )

        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)
        skill_reviewer = MagicMock(spec=SkillReviewer)

        responsible_agent = Agent(
            agent_id="lead-1",
            soul=Soul(system_prompt="test"),
            agent_type="workspace_bound",
        )

        captured_messages: list[list[dict]] = []
        call_index = 0

        # LLM script: huge_output → task_done
        responses = [
            _make_response(
                tool_calls=[ToolCall(id="c1", name="huge_output", arguments={"query": "x"})],
            ),
            _make_response(
                tool_calls=[ToolCall(id="c2", name="task_done", arguments={})],
            ),
        ]

        def capturing_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_index
            captured_messages.append(list(request.messages))
            idx = call_index
            call_index += 1
            return responses[idx] if idx < len(responses) else responses[-1]

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-test",
            responsible_agent=responsible_agent,
            call_llm=capturing_llm,
            system_llm=MagicMock(return_value=_make_response(content="summary")),
            free_agent_manager=free_agent_manager,
            skill_reviewer=skill_reviewer,
            max_tool_output_chars=10000,
            extra_actions=[HugeOutputAction()],
        )
        ws.receive_budget(100000)

        issue = Issue(issue_id="test-1", repo="test/repo", description="Fix bug")
        ws.execute(issue)

        # Find the tool result in captured messages
        all_tool_msgs = []
        for msgs in captured_messages:
            for m in msgs:
                if m.get("role") == "tool" and m.get("tool_call_id") == "c1":
                    all_tool_msgs.append(m)

        assert len(all_tool_msgs) >= 1
        content = all_tool_msgs[0]["content"]
        assert len(content) < 15000, (
            f"Workspace should pass truncation config to agent, "
            f"but tool output was {len(content)} chars"
        )
        assert "characters were elided" in content


# ===========================================================================
# IT: Compaction triggers during long agent loop
# ===========================================================================


@pytest.mark.integration
class TestCompactionInAgentLoop:
    """When conversation tokens approach the context window limit,
    the agent must trigger compaction via system_llm."""

    def test_compaction_triggers_when_context_fills(self):
        """After many iterations, when estimated tokens exceed 90% of
        max_context_tokens, compaction should fire and compress history."""
        from llm_agent_toolkit.stdlib.action import Action

        class FillContextAction(Action):
            """Returns medium output to gradually fill context."""
            @property
            def name(self): return "fill"
            @property
            def description(self): return "Fills context"
            @property
            def parameters(self): return {"x": {"type": "string", "required": True}}
            def execute(self, **kwargs):
                return "data " * 500  # ~2500 chars per call

        call_count = 0
        compaction_called = False

        def fake_system_llm(request: LLMRequest) -> LLMResponse:
            nonlocal compaction_called
            # Check if this is a compaction request
            for m in request.messages:
                if "CONTEXT CHECKPOINT" in m.get("content", ""):
                    compaction_called = True
            return _make_response(content="Summary: searched code and found bug in foo.py")

        responses_iter = iter(
            # 20 iterations of fill, then task_done
            [_make_response(tool_calls=[ToolCall(id=f"c{i}", name="fill", arguments={"x": "y"})]) for i in range(20)]
            + [_make_response(tool_calls=[ToolCall(id="done", name="task_done", arguments={})])]
        )

        def fake_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            return next(responses_iter)

        agent = ReactAgent(
            system_prompt="test agent",
            actions=[FillContextAction(), TaskDoneAction()],
            call_llm=fake_llm,
            max_iterations=25,
            max_tool_output_chars=5000,
            max_context_tokens=5000,  # very small to trigger compaction quickly
            system_llm=fake_system_llm,
        )
        result = agent.run(context="Fix the bug")

        # Compaction should have been triggered at some point
        assert compaction_called, (
            "Compaction should trigger when context approaches max_context_tokens"
        )
        # Agent should still complete
        assert result.termination_reason == "done"

    def test_no_compaction_when_context_fits(self):
        """With large context window, no compaction triggers."""
        compaction_called = False

        def fake_system_llm(request: LLMRequest) -> LLMResponse:
            nonlocal compaction_called
            for m in request.messages:
                if "CONTEXT CHECKPOINT" in m.get("content", ""):
                    compaction_called = True
            return _make_response(content="summary")

        call_count = 0

        def fake_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(
                    tool_calls=[ToolCall(id="c1", name="task_done", arguments={})],
                )
            return _make_response(content="done")

        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=fake_llm,
            max_iterations=5,
            max_context_tokens=262144,  # huge — no compaction needed
            system_llm=fake_system_llm,
        )
        result = agent.run(context="simple task")

        assert not compaction_called
        assert result.termination_reason == "done"
