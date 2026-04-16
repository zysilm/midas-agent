"""Integration tests for action log pipeline: workspace and TUI produce JSONL."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.action import Action
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.types import Issue
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.skill import SkillReviewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBashAction(Action):
    """Bash action stub for integration tests."""

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Run bash"

    @property
    def parameters(self) -> dict:
        return {"command": {"type": "string", "required": True}}

    def execute(self, **kwargs) -> str:
        return "fake output"


def _usage(n: int = 10) -> TokenUsage:
    return TokenUsage(input_tokens=n, output_tokens=n)


def _make_llm_response(content="ok", tool_calls=None):
    return LLMResponse(content=content, tool_calls=tool_calls, usage=_usage())


def _task_done_response(result="done"):
    return _make_llm_response(
        content=None,
        tool_calls=[ToolCall(id="tc-done", name="task_done", arguments={"result": result})],
    )


def _bash_then_done_responses():
    """LLM responses: one bash call, then task_done."""
    return [
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})],
            usage=_usage(),
        ),
        # PlanExecuteAgent planning phase response (content only, no tool calls)
        _make_llm_response(content="Plan: run bash then finish"),
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c2", name="bash", arguments={"command": "echo world"})],
            usage=_usage(),
        ),
        _task_done_response("all done"),
    ]


FAKE_ISSUE = Issue(
    issue_id="issue-test-log",
    repo="tests/fixtures/sample_repo",
    description="Test issue for action log.",
    fail_to_pass=["tests/test_x.py::test_a"],
    pass_to_pass=[],
)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTrainingEpisodeActionLog:
    """Workspace passes action_log through to the agent."""

    def test_training_episode_produces_action_log(self, tmp_path):
        """After ws.execute(issue), the action_log file contains valid JSONL entries."""
        from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
        from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace

        log_path = tmp_path / "action_log.jsonl"

        # Scripted LLM that calls bash then task_done
        call_count = 0
        responses = _bash_then_done_responses()

        def scripted_llm(req: LLMRequest) -> LLMResponse:
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx < len(responses):
                return responses[idx]
            return responses[-1]

        # Build minimal workspace
        agent = Agent(
            agent_id="resp-1",
            soul=Soul(system_prompt="You are a test agent."),
            agent_type="workspace_bound",
        )

        pricing = MagicMock()
        pricing.calculate_price = MagicMock(return_value=100)
        fam = FreeAgentManager(pricing_engine=pricing)
        skill_reviewer = MagicMock(spec=SkillReviewer)
        skill_reviewer.review = MagicMock()

        with open(log_path, "w") as action_log_file:
            ws = GraphEmergenceWorkspace(
                workspace_id="ws-test-log",
                responsible_agent=agent,
                call_llm=scripted_llm,
                system_llm=scripted_llm,
                free_agent_manager=fam,
                skill_reviewer=skill_reviewer,
                action_overrides={"bash": FakeBashAction()},
                action_log=action_log_file,
            )
            ws.receive_budget(100000)
            ws.execute(FAKE_ISSUE)

        # Verify JSONL file has content
        assert log_path.exists()
        text = log_path.read_text()
        lines = [line for line in text.strip().splitlines() if line.strip()]
        assert len(lines) >= 1, f"Expected at least 1 JSONL line, got {len(lines)}"

        # Each line is valid JSON with expected fields
        for line in lines:
            entry = json.loads(line)
            assert "iter" in entry
            assert "action" in entry
            assert "result" in entry
            assert "timestamp" in entry


@pytest.mark.integration
class TestTUISessionActionLog:
    """TUI passes action_log through to the ReactAgent it creates."""

    def test_tui_session_produces_action_log(self, tmp_path):
        """TUI with action_log file produces JSONL entries for executed actions."""
        from midas_agent.tui import TUI

        log_path = tmp_path / "tui_action_log.jsonl"

        call_llm = MagicMock(return_value=_task_done_response("fixed"))

        with open(log_path, "w") as action_log_file:
            tui = TUI(
                call_llm=call_llm,
                actions=[FakeBashAction(), TaskDoneAction()],
                system_prompt="test agent",
                action_log=action_log_file,
            )

            with patch("builtins.input", side_effect=["Fix the bug", "/quit"]):
                tui.run()

        # Verify JSONL file has content
        assert log_path.exists()
        text = log_path.read_text()
        lines = [line for line in text.strip().splitlines() if line.strip()]
        assert len(lines) >= 1, f"Expected at least 1 JSONL line, got {len(lines)}"

        # Each line is valid JSON
        for line in lines:
            entry = json.loads(line)
            assert "action" in entry
            assert "timestamp" in entry
            assert isinstance(entry["timestamp"], float)
