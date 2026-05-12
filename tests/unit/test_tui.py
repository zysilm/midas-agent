"""Unit tests for TUI components: on_action callback and REPL behavior.

Tests define the target behavior of:
- ReactAgent on_action callback: real-time notification of action execution
- TUI class: REPL loop, command handling, output rendering

Tests are expected to FAIL until the TUI module is implemented.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call
from io import StringIO

import pytest

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from llm_agent_toolkit.stdlib.actions.task_done import TaskDoneAction
from llm_agent_toolkit.stdlib.react_agent import ReactAgent, ActionRecord
from midas_agent.tui import TUI, ActionEvent


# ===================================================================
# on_action callback on ReactAgent
# ===================================================================


def _make_llm_response(content="done", tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _make_tool_call(name="task_done", arguments=None):
    return ToolCall(id="tc-1", name=name, arguments=arguments or {"result": "ok"})


@pytest.mark.unit
class TestOnActionCallback:
    """ReactAgent must call on_action for each action it executes."""

    def test_on_action_called_for_each_action(self):
        """on_action is invoked once per tool call execution."""
        events = []

        def on_action(event: ActionEvent):
            events.append(event)

        call_llm = MagicMock(
            return_value=_make_llm_response(
                content=None,
                tool_calls=[_make_tool_call("task_done", {"result": "fixed"})],
            )
        )

        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=call_llm,
            on_action=on_action,
        )
        agent.run(context="Fix the bug")

        assert len(events) == 1
        assert events[0].action_name == "task_done"

    def test_on_action_receives_arguments(self):
        """ActionEvent includes the arguments passed to the action."""
        events = []
        call_llm = MagicMock(
            return_value=_make_llm_response(
                content=None,
                tool_calls=[_make_tool_call("task_done", {"result": "done"})],
            )
        )

        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=call_llm,
            on_action=lambda e: events.append(e),
        )
        agent.run(context="test")

        assert events[0].arguments == {"result": "done"}

    def test_on_action_receives_result(self):
        """ActionEvent includes the string result from action.execute()."""
        events = []
        call_llm = MagicMock(
            return_value=_make_llm_response(
                content=None,
                tool_calls=[_make_tool_call("task_done", {"result": "patched"})],
            )
        )

        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=call_llm,
            on_action=lambda e: events.append(e),
        )
        agent.run(context="test")

        assert events[0].result is not None
        assert isinstance(events[0].result, str)

    def test_no_on_action_still_works(self):
        """Backward compat: on_action=None (default) doesn't break."""
        call_llm = MagicMock(
            return_value=_make_llm_response(
                content=None,
                tool_calls=[_make_tool_call("task_done", {"result": "ok"})],
            )
        )

        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=call_llm,
            # no on_action parameter
        )
        result = agent.run(context="test")
        assert result.termination_reason == "done"

    def test_on_action_default_is_none(self):
        """ReactAgent.__init__ accepts on_action as optional kwarg, defaults to None."""
        agent = ReactAgent(
            system_prompt="test",
            actions=[],
            call_llm=MagicMock(),
        )
        assert getattr(agent, "on_action", None) is None or getattr(agent, "_on_action", None) is None


# ===================================================================
# ActionEvent dataclass
# ===================================================================


@pytest.mark.unit
class TestActionEvent:
    """ActionEvent captures all information about an executed action."""

    def test_has_required_fields(self):
        event = ActionEvent(action_name="bash", arguments={"command": "ls"}, result="file.py")
        assert event.action_name == "bash"
        assert event.arguments == {"command": "ls"}
        assert event.result == "file.py"

    def test_action_event_is_dataclass_or_model(self):
        """ActionEvent can be constructed with keyword arguments."""
        event = ActionEvent(action_name="read_file", arguments={"path": "f.py"}, result="content")
        assert event.action_name == "read_file"


# ===================================================================
# TUI REPL
# ===================================================================


@pytest.mark.unit
class TestTUIRepl:
    """TUI provides a REPL that reads input, runs agent, displays output."""

    def test_quit_command_exits(self):
        """Typing /quit exits the REPL loop."""
        tui = TUI(
            call_llm=MagicMock(),
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        with patch("builtins.input", side_effect=["/quit"]):
            tui.run()  # Should return without error

    def test_exit_command_exits(self):
        """Typing /exit also exits the REPL loop."""
        tui = TUI(
            call_llm=MagicMock(),
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        with patch("builtins.input", side_effect=["/exit"]):
            tui.run()

    def test_empty_input_ignored(self):
        """Empty input (just Enter) is ignored, loop continues."""
        tui = TUI(
            call_llm=MagicMock(),
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        # Empty string, then /quit
        with patch("builtins.input", side_effect=["", "   ", "/quit"]):
            tui.run()  # Should not crash

    def test_runs_agent_with_user_input(self):
        """User's text is passed as context to the agent."""
        call_llm = MagicMock(
            return_value=_make_llm_response(
                content=None,
                tool_calls=[_make_tool_call("task_done", {"result": "done"})],
            )
        )
        tui = TUI(
            call_llm=call_llm,
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        with patch("builtins.input", side_effect=["Fix the bug", "/quit"]):
            tui.run()

        # LLM was called with the user's input as context
        assert call_llm.call_count >= 1
        first_call = call_llm.call_args_list[0]
        request = first_call[0][0]
        messages_content = " ".join(m.get("content", "") for m in request.messages)
        assert "Fix the bug" in messages_content

    def test_displays_welcome_message(self, capsys):
        """TUI prints a welcome message on startup."""
        tui = TUI(
            call_llm=MagicMock(),
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        with patch("builtins.input", side_effect=["/quit"]):
            tui.run()

        captured = capsys.readouterr()
        assert "midas" in captured.out.lower() or "agent" in captured.out.lower()

    def test_eof_exits_gracefully(self):
        """Ctrl+D (EOFError) exits the REPL without traceback."""
        tui = TUI(
            call_llm=MagicMock(),
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        with patch("builtins.input", side_effect=EOFError):
            tui.run()  # Should not raise

    def test_keyboard_interrupt_exits_gracefully(self):
        """Ctrl+C (KeyboardInterrupt) exits the REPL without traceback."""
        tui = TUI(
            call_llm=MagicMock(),
            actions=[TaskDoneAction()],
            system_prompt="test",
        )
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            tui.run()  # Should not raise
