"""Unit tests for stuck detection in ReactAgent.

Tests verify that _check_stuck detects repetitive patterns in action history
and that ReactAgent injects a warning message when stuck is detected.

Tests are expected to FAIL until stuck detection is implemented.
"""
from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from midas_agent.stdlib.react_agent import ActionRecord, ReactAgent


def _record(name: str, args: dict | None = None, result: str = "ok") -> ActionRecord:
    return ActionRecord(
        action_name=name,
        arguments=args or {},
        result=result,
        timestamp=1000.0,
    )


# ===================================================================
# _check_stuck pure function tests
# ===================================================================


@pytest.mark.unit
class TestCheckStuck:
    """_check_stuck detects repetitive action patterns."""

    def test_detects_same_file_edited_3_times(self):
        history = [
            _record("edit_file", {"path": "core.py", "old_string": "a", "new_string": "b"}),
            _record("bash", {"command": "python test.py"}, "FAILED"),
            _record("edit_file", {"path": "core.py", "old_string": "b", "new_string": "c"}),
            _record("bash", {"command": "python test.py"}, "FAILED"),
            _record("edit_file", {"path": "core.py", "old_string": "c", "new_string": "d"}),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is not None
        assert "core.py" in result

    def test_detects_same_file_edited_5_times(self):
        history = [
            _record("edit_file", {"path": "utils.py", "old_string": str(i), "new_string": str(i + 1)})
            for i in range(5)
        ]
        result = ReactAgent._check_stuck(history)
        assert result is not None

    def test_no_false_positive_different_files(self):
        history = [
            _record("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}),
            _record("edit_file", {"path": "b.py", "old_string": "x", "new_string": "y"}),
            _record("edit_file", {"path": "c.py", "old_string": "x", "new_string": "y"}),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is None

    def test_no_false_positive_mixed_actions(self):
        history = [
            _record("search_code", {"pattern": "bug"}),
            _record("read_file", {"path": "core.py"}),
            _record("edit_file", {"path": "core.py", "old_string": "a", "new_string": "b"}),
            _record("bash", {"command": "pytest"}),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is None

    def test_no_false_positive_short_history(self):
        history = [
            _record("edit_file", {"path": "core.py", "old_string": "a", "new_string": "b"}),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is None

    def test_no_false_positive_empty_history(self):
        result = ReactAgent._check_stuck([])
        assert result is None

    def test_detects_identical_action_repeated_3_times(self):
        """Exact same action+args 3 times."""
        history = [
            _record("bash", {"command": "python test.py"}, "FAILED"),
            _record("bash", {"command": "python test.py"}, "FAILED"),
            _record("bash", {"command": "python test.py"}, "FAILED"),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is not None

    def test_no_false_positive_similar_but_different_commands(self):
        history = [
            _record("bash", {"command": "python test_a.py"}, "FAILED"),
            _record("bash", {"command": "python test_b.py"}, "PASSED"),
            _record("bash", {"command": "python test_c.py"}, "FAILED"),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is None

    def test_detects_edit_test_cycle(self):
        """edit same file → test fails → edit same file → test fails → edit same file."""
        history = [
            _record("edit_file", {"path": "core.py", "old_string": "v1", "new_string": "v2"}),
            _record("bash", {"command": "pytest test_core.py"}, "FAILED"),
            _record("edit_file", {"path": "core.py", "old_string": "v2", "new_string": "v3"}),
            _record("bash", {"command": "pytest test_core.py"}, "FAILED"),
            _record("edit_file", {"path": "core.py", "old_string": "v3", "new_string": "v4"}),
            _record("bash", {"command": "pytest test_core.py"}, "FAILED"),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is not None

    def test_returns_string_with_guidance(self):
        """Stuck message should tell agent to use think tool."""
        history = [
            _record("edit_file", {"path": "core.py", "old_string": str(i), "new_string": str(i + 1)})
            for i in range(3)
        ]
        result = ReactAgent._check_stuck(history)
        assert "think" in result.lower()

    def test_two_edits_not_stuck(self):
        """2 edits to same file is normal, not stuck."""
        history = [
            _record("edit_file", {"path": "core.py", "old_string": "a", "new_string": "b"}),
            _record("bash", {"command": "pytest"}, "FAILED"),
            _record("edit_file", {"path": "core.py", "old_string": "b", "new_string": "c"}),
        ]
        result = ReactAgent._check_stuck(history)
        assert result is None


# ===================================================================
# ReactAgent integration: stuck warning injected into messages
# ===================================================================


@pytest.mark.unit
class TestStuckWarningInjection:
    """ReactAgent injects a warning message when stuck is detected."""

    def test_warning_injected_after_stuck(self):
        """After 3 edits to same file, the LLM receives a warning in messages."""
        from midas_agent.llm.types import LLMResponse, TokenUsage, ToolCall
        from midas_agent.stdlib.actions.task_done import TaskDoneAction
        from midas_agent.stdlib.action import Action

        class FakeEditAction(Action):
            @property
            def name(self): return "edit_file"
            @property
            def description(self): return "Edit"
            @property
            def parameters(self): return {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}
            def execute(self, **kwargs): return "Edited"

        call_count = {"n": 0}
        warning_seen = {"value": False}

        def fake_call_llm(request):
            call_count["n"] += 1
            messages_text = " ".join(m.get("content", "") or "" for m in request.messages)

            # Check if stuck warning was injected
            if "you have edited" in messages_text.lower() or "stuck" in messages_text.lower():
                warning_seen["value"] = True
                # After warning, use think then task_done
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id=f"c{call_count['n']}", name="task_done",
                                        arguments={"result": "done"})],
                    usage=TokenUsage(500, 100),
                )

            # Keep editing same file to trigger stuck detection
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id=f"c{call_count['n']}", name="edit_file",
                                    arguments={"path": "core.py",
                                              "old_string": f"v{call_count['n']}",
                                              "new_string": f"v{call_count['n']+1}"})],
                usage=TokenUsage(500, 100),
            )

        agent = ReactAgent(
            system_prompt="test",
            actions=[FakeEditAction(), TaskDoneAction()],
            call_llm=fake_call_llm,
            max_iterations=20,
        )
        agent.run(context="Fix the bug")

        assert warning_seen["value"], \
            "Stuck warning was never injected into LLM messages"
