"""Unit tests for action log: untruncated JSONL output from ReactAgent."""
import io
import json
import time

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.action import Action
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.react_agent import ReactAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBashAction(Action):
    """Bash action stub that returns a configurable string."""

    def __init__(self, output: str = "hello"):
        self._output = output

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
        return self._output


def _make_scripted_llm(responses: list[LLMResponse]):
    """Return a callable that yields responses in order."""
    call_index = 0

    def call_llm(req: LLMRequest) -> LLMResponse:
        nonlocal call_index
        idx = call_index
        call_index += 1
        if idx < len(responses):
            return responses[idx]
        return responses[-1]

    return call_llm


def _usage() -> TokenUsage:
    return TokenUsage(input_tokens=10, output_tokens=10)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestActionLog:
    """Tests for the action_log parameter on ReactAgent."""

    def test_action_log_writes_jsonl(self):
        """Providing action_log (StringIO) causes one JSON line per action."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="task_done", arguments={"result": "done"})],
                usage=_usage(),
            ),
        ]
        log_buf = io.StringIO()
        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            action_log=log_buf,
        )
        agent.run()

        log_buf.seek(0)
        lines = [line for line in log_buf.readlines() if line.strip()]
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert "iter" in entry
        assert "action" in entry
        assert "args" in entry
        assert "result" in entry
        assert "timestamp" in entry

    def test_action_log_result_not_truncated(self):
        """Action log stores the FULL result even when max_tool_output_chars truncates it."""
        long_output = "x" * 200

        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo long"})],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c2", name="task_done", arguments={"result": "done"})],
                usage=_usage(),
            ),
        ]
        log_buf = io.StringIO()
        agent = ReactAgent(
            system_prompt="test",
            actions=[FakeBashAction(output=long_output), TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            max_tool_output_chars=50,
            action_log=log_buf,
        )
        agent.run()

        log_buf.seek(0)
        lines = [line for line in log_buf.readlines() if line.strip()]
        assert len(lines) >= 1

        # First line is the bash action with the full (untruncated) result
        bash_entry = json.loads(lines[0])
        assert bash_entry["action"] == "bash"
        assert len(bash_entry["result"]) == 200
        assert bash_entry["result"] == long_output

    def test_action_log_multiple_actions(self):
        """Agent executing 2 tool calls then task_done produces 3 JSONL lines."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo a"})],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c2", name="bash", arguments={"command": "echo b"})],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c3", name="task_done", arguments={"result": "done"})],
                usage=_usage(),
            ),
        ]
        log_buf = io.StringIO()
        agent = ReactAgent(
            system_prompt="test",
            actions=[FakeBashAction(output="ok"), TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            action_log=log_buf,
        )
        agent.run()

        log_buf.seek(0)
        lines = [line for line in log_buf.readlines() if line.strip()]
        assert len(lines) == 3

        # Each line must be valid JSON
        for line in lines:
            entry = json.loads(line)
            assert "action" in entry

    def test_no_action_log_still_works(self):
        """action_log=None (default) causes no crash; agent runs normally."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="task_done", arguments={"result": "done"})],
                usage=_usage(),
            ),
        ]
        agent = ReactAgent(
            system_prompt="test",
            actions=[TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            action_log=None,
        )
        result = agent.run()
        assert result.termination_reason == "done"

    def test_action_log_contains_args(self):
        """The args field matches the tool_call arguments exactly."""
        expected_args = {"command": "ls -la /tmp"}
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="bash", arguments=expected_args)],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c2", name="task_done", arguments={"result": "ok"})],
                usage=_usage(),
            ),
        ]
        log_buf = io.StringIO()
        agent = ReactAgent(
            system_prompt="test",
            actions=[FakeBashAction(output="file.txt"), TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            action_log=log_buf,
        )
        agent.run()

        log_buf.seek(0)
        lines = [line for line in log_buf.readlines() if line.strip()]
        bash_entry = json.loads(lines[0])
        assert bash_entry["args"] == expected_args

    def test_action_log_has_timestamp(self):
        """Each JSONL line has a timestamp field that is a positive float."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "date"})],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c2", name="task_done", arguments={"result": "done"})],
                usage=_usage(),
            ),
        ]
        log_buf = io.StringIO()
        agent = ReactAgent(
            system_prompt="test",
            actions=[FakeBashAction(output="now"), TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            action_log=log_buf,
        )

        before = time.time()
        agent.run()
        after = time.time()

        log_buf.seek(0)
        lines = [line for line in log_buf.readlines() if line.strip()]
        for line in lines:
            entry = json.loads(line)
            ts = entry["timestamp"]
            assert isinstance(ts, float)
            assert ts > 0
            assert before <= ts <= after

    def test_action_log_iter_increments(self):
        """JSONL lines have iter 1, 2, 3... matching iteration count."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo 1"})],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c2", name="bash", arguments={"command": "echo 2"})],
                usage=_usage(),
            ),
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c3", name="task_done", arguments={"result": "done"})],
                usage=_usage(),
            ),
        ]
        log_buf = io.StringIO()
        agent = ReactAgent(
            system_prompt="test",
            actions=[FakeBashAction(output="ok"), TaskDoneAction()],
            call_llm=_make_scripted_llm(responses),
            action_log=log_buf,
        )
        agent.run()

        log_buf.seek(0)
        lines = [line for line in log_buf.readlines() if line.strip()]
        iters = [json.loads(line)["iter"] for line in lines]
        assert iters == [1, 2, 3]
