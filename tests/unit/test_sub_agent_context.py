"""Tests for sub-agent context inheritance."""
import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.file_ops import ReadFileAction
from midas_agent.stdlib.actions.search import SearchCodeAction, FindFilesAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.actions.report_result import ReportResultAction


def _make_response(content=None, tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


class TestSubAgentGetsParentSystemPrompt:
    """Sub-agents must receive the parent's system prompt, not a generic one."""

    def test_sub_agent_gets_parent_system_prompt(self):
        """When DelegateTaskAction spawns a sub-agent, the ReactAgent uses
        the parent's system prompt."""
        captured = {}

        def fake_llm(request: LLMRequest) -> LLMResponse:
            # Capture the system prompt from the first message
            if request.messages and request.messages[0]["role"] == "system":
                captured["system_prompt"] = request.messages[0]["content"]
            # Return report_result to end the sub-agent
            return _make_response(
                tool_calls=[ToolCall(
                    id="c1", name="report_result",
                    arguments={"result": "found it at /testbed/foo.py"},
                )],
            )

        parent_system_prompt = "You are a coding agent that solves issues..."
        parent_actions = [
            BashAction(cwd="/testbed"),
            ReadFileAction(cwd="/testbed"),
            SearchCodeAction(cwd="/testbed"),
            FindFilesAction(cwd="/testbed"),
            TaskDoneAction(),
        ]

        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
            spawn_callback=lambda desc: type("Agent", (), {
                "agent_id": "spawned-test",
                "protected_by": "parent-1",
                "soul": type("Soul", (), {"system_prompt": parent_system_prompt})(),
                "skill": None,
            })(),
            call_llm=fake_llm,
            parent_actions=parent_actions,
            calling_agent_id="parent-1",
            parent_system_prompt=parent_system_prompt,
        )

        action.execute(
            task_description="Find where foo is defined",
            spawn=["explorer: find foo"],
        )

        assert "system_prompt" in captured, "Sub-agent LLM should have been called"
        assert captured["system_prompt"] == parent_system_prompt, (
            f"Sub-agent should get parent's system prompt, got: {captured['system_prompt'][:100]}"
        )


class TestSubAgentGetsEnvironmentContext:
    """Sub-agent's task context must include environment context with cwd."""

    def test_sub_agent_gets_environment_context(self):
        """Sub-agent's user message includes <environment_context> with cwd."""
        captured = {}

        def fake_llm(request: LLMRequest) -> LLMResponse:
            # Capture all user messages
            user_msgs = [m for m in request.messages if m["role"] == "user"]
            if user_msgs:
                captured["user_content"] = user_msgs[-1]["content"]
            return _make_response(
                tool_calls=[ToolCall(
                    id="c1", name="report_result",
                    arguments={"result": "done"},
                )],
            )

        parent_actions = [
            BashAction(cwd="/testbed"),
            ReadFileAction(cwd="/testbed"),
            SearchCodeAction(cwd="/testbed"),
            FindFilesAction(cwd="/testbed"),
            TaskDoneAction(),
        ]

        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
            spawn_callback=lambda desc: type("Agent", (), {
                "agent_id": "spawned-test",
                "protected_by": "parent-1",
                "soul": type("Soul", (), {"system_prompt": "test"})(),
                "skill": None,
            })(),
            call_llm=fake_llm,
            parent_actions=parent_actions,
            calling_agent_id="parent-1",
            parent_system_prompt="test",
            env_context_xml="<environment_context>\n  <cwd>/testbed</cwd>\n</environment_context>",
        )

        action.execute(
            task_description="Search for function X",
            spawn=["explorer: search for X"],
        )

        assert "user_content" in captured
        assert "<cwd>/testbed</cwd>" in captured["user_content"], (
            f"Sub-agent should see cwd in environment context, got: {captured['user_content'][:200]}"
        )


class TestSubAgentMaxIterations:
    """Sub-agents must be capped at 20 iterations."""

    def test_sub_agent_max_iterations_capped(self):
        """Sub-agent ReactAgent is created with max_iterations=20."""
        call_count = {"n": 0}

        def fake_llm(request: LLMRequest) -> LLMResponse:
            call_count["n"] += 1
            if call_count["n"] >= 25:
                # Safety: should never reach here if capped at 20
                return _make_response(
                    tool_calls=[ToolCall(
                        id="done", name="report_result",
                        arguments={"result": "done"},
                    )],
                )
            # Keep calling bash to burn iterations
            return _make_response(
                tool_calls=[ToolCall(
                    id=f"c{call_count['n']}", name="bash",
                    arguments={"command": "echo hello"},
                )],
            )

        parent_actions = [
            BashAction(cwd="/testbed"),
            ReadFileAction(cwd="/testbed"),
            TaskDoneAction(),
        ]

        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
            spawn_callback=lambda desc: type("Agent", (), {
                "agent_id": "spawned-test",
                "protected_by": "parent-1",
                "soul": type("Soul", (), {"system_prompt": "test"})(),
                "skill": None,
            })(),
            call_llm=fake_llm,
            parent_actions=parent_actions,
            calling_agent_id="parent-1",
            parent_system_prompt="test",
        )

        action.execute(
            task_description="Investigate the bug",
            spawn=["explorer: investigate"],
        )

        # Sub-agent should stop at 20 iterations, not run to 9999
        assert call_count["n"] <= 20, (
            f"Sub-agent ran {call_count['n']} iterations, should be capped at 20"
        )
