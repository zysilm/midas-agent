"""Unit tests for ReactAgent, AgentResult, and ActionRecord."""
import pytest

from midas_agent.stdlib.react_agent import ReactAgent, AgentResult, ActionRecord
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


def _dummy_call_llm(req: LLMRequest) -> LLMResponse:
    """Stub LLM callback that returns an empty response."""
    return LLMResponse(
        content="done",
        tool_calls=None,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.mark.unit
class TestAgentResult:
    """Tests for the AgentResult dataclass."""

    def test_agent_result_fields(self):
        """AgentResult stores output, iterations, termination_reason, and action_history."""
        record = ActionRecord(
            action_name="bash",
            arguments={"command": "ls"},
            result="file.txt",
            timestamp=1000.0,
        )
        result = AgentResult(
            output="task completed",
            iterations=3,
            termination_reason="done",
            action_history=[record],
        )

        assert result.output == "task completed"
        assert result.iterations == 3
        assert result.termination_reason == "done"
        assert len(result.action_history) == 1
        assert result.action_history[0].action_name == "bash"

    def test_agent_result_default_history(self):
        """AgentResult defaults action_history to an empty list."""
        result = AgentResult(
            output="ok",
            iterations=1,
            termination_reason="done",
        )

        assert result.action_history == []


@pytest.mark.unit
class TestActionRecord:
    """Tests for the ActionRecord dataclass."""

    def test_action_record_fields(self):
        """ActionRecord stores action_name, arguments, result, and timestamp."""
        record = ActionRecord(
            action_name="read_file",
            arguments={"file_path": "/tmp/test.txt"},
            result="file content here",
            timestamp=1234567890.0,
        )

        assert record.action_name == "read_file"
        assert record.arguments == {"file_path": "/tmp/test.txt"}
        assert record.result == "file content here"
        assert record.timestamp == 1234567890.0


@pytest.mark.unit
class TestReactAgent:
    """Tests for ReactAgent construction and run behavior."""

    def test_construction(self):
        """ReactAgent can be constructed with system_prompt, actions, call_llm, and max_iterations."""
        agent = ReactAgent(
            system_prompt="You are a helpful agent.",
            actions=[],
            call_llm=_dummy_call_llm,
            max_iterations=10,
        )
        assert agent is not None

    def test_run_returns_agent_result(self):
        """run() returns an AgentResult instance."""
        agent = ReactAgent(
            system_prompt="You are a helpful agent.",
            actions=[],
            call_llm=_dummy_call_llm,
        )
        result = agent.run()
        assert isinstance(result, AgentResult)

    def test_run_with_context(self):
        """run() accepts a context string parameter."""
        agent = ReactAgent(
            system_prompt="You are a helpful agent.",
            actions=[],
            call_llm=_dummy_call_llm,
        )
        result = agent.run(context="Some additional context for the task.")
        assert isinstance(result, AgentResult)

    def test_terminates_on_budget_exhausted(self):
        """When BudgetExhaustedError is raised, termination_reason is 'budget_exhausted'."""
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError

        call_count = 0

        def exhausting_llm(req: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            raise BudgetExhaustedError("No budget remaining")

        agent = ReactAgent(
            system_prompt="You are a helpful agent.",
            actions=[],
            call_llm=exhausting_llm,
        )
        result = agent.run()
        assert result.termination_reason == "budget_exhausted"

    def test_terminates_on_max_iterations(self):
        """Agent stops when max_iterations is reached and sets termination_reason='max_iterations'."""
        agent = ReactAgent(
            system_prompt="You are a helpful agent.",
            actions=[],
            call_llm=_dummy_call_llm,
            max_iterations=1,
        )
        result = agent.run()
        assert result.termination_reason == "max_iterations"
        assert result.iterations <= 1

    def test_passes_tools_to_llm_request(self):
        """run() passes action definitions as tools in the LLMRequest."""
        from midas_agent.stdlib.actions.bash import BashAction
        from midas_agent.stdlib.actions.task_done import TaskDoneAction

        requests_log: list[LLMRequest] = []

        def capturing_llm(req: LLMRequest) -> LLMResponse:
            requests_log.append(req)
            return LLMResponse(
                content="done",
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

        agent = ReactAgent(
            system_prompt="test",
            actions=[BashAction(), TaskDoneAction()],
            call_llm=capturing_llm,
        )
        agent.run()

        assert len(requests_log) >= 1
        tools = requests_log[0].tools
        assert tools is not None
        assert len(tools) == 2
        tool_names = {t["function"]["name"] for t in tools}
        assert tool_names == {"bash", "task_done"}

    def test_tools_have_correct_schema(self):
        """Tools passed to LLM follow the OpenAI function calling format."""
        from midas_agent.stdlib.actions.bash import BashAction

        requests_log: list[LLMRequest] = []

        def capturing_llm(req: LLMRequest) -> LLMResponse:
            requests_log.append(req)
            return LLMResponse(
                content="done", tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

        agent = ReactAgent(
            system_prompt="test",
            actions=[BashAction()],
            call_llm=capturing_llm,
        )
        agent.run()

        tool = requests_log[0].tools[0]
        assert tool["type"] == "function"
        assert "name" in tool["function"]
        assert "description" in tool["function"]
        assert "parameters" in tool["function"]
        params = tool["function"]["parameters"]
        assert params["type"] == "object"
        assert "command" in params["properties"]
        assert "command" in params["required"]

    def test_no_tools_when_no_actions(self):
        """When agent has no actions, tools is None."""
        requests_log: list[LLMRequest] = []

        def capturing_llm(req: LLMRequest) -> LLMResponse:
            requests_log.append(req)
            return LLMResponse(
                content="done", tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

        agent = ReactAgent(
            system_prompt="test",
            actions=[],
            call_llm=capturing_llm,
        )
        agent.run()

        assert requests_log[0].tools is None

    def test_executes_tool_call_and_returns_result(self):
        """Agent executes tool calls from LLM and feeds results back."""
        from midas_agent.stdlib.actions.bash import BashAction
        from midas_agent.llm.types import ToolCall

        call_count = 0

        def llm_with_tool_then_done(req: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                )
            return LLMResponse(
                content="done", tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

        agent = ReactAgent(
            system_prompt="test",
            actions=[BashAction()],
            call_llm=llm_with_tool_then_done,
        )
        result = agent.run()

        assert result.termination_reason == "done"
        assert len(result.action_history) == 1
        assert result.action_history[0].action_name == "bash"
        assert "hello" in result.action_history[0].result

    def test_terminates_on_task_done(self):
        """When TaskDoneAction is invoked, termination_reason is 'done'."""
        from midas_agent.stdlib.actions.task_done import TaskDoneAction
        from midas_agent.llm.types import ToolCall

        def llm_that_calls_task_done(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="task_done",
                        arguments={"summary": "All done"},
                    )
                ],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

        agent = ReactAgent(
            system_prompt="You are a helpful agent.",
            actions=[TaskDoneAction()],
            call_llm=llm_that_calls_task_done,
        )
        result = agent.run()
        assert result.termination_reason == "done"
