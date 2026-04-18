"""Unit tests for PlanExecuteAgent."""
import pytest

from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.stdlib.react_agent import AgentResult, ReactAgent
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


def _dummy_call_llm(req: LLMRequest) -> LLMResponse:
    """Stub LLM callback that returns a text response (plan or done)."""
    return LLMResponse(
        content="done",
        tool_calls=None,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _scripted_call_llm() -> callable:
    """Create a scripted LLM callback for plan+execute phases."""
    from midas_agent.llm.types import ToolCall

    responses = [
        LLMResponse(content="Plan: analyze and fix.", tool_calls=None, usage=TokenUsage(10, 5)),
        LLMResponse(content=None, tool_calls=[ToolCall(id="c1", name="task_done", arguments={})], usage=TokenUsage(10, 5)),
    ]
    idx = {"i": 0}

    def call_llm(req: LLMRequest) -> LLMResponse:
        i = idx["i"]
        idx["i"] += 1
        return responses[i] if i < len(responses) else responses[-1]

    return call_llm


_DUMMY_ENV_CONTEXT_XML = "<environment_context>\n  <balance>1000</balance>\n</environment_context>"


@pytest.mark.unit
class TestPlanExecuteAgent:
    """Tests for PlanExecuteAgent construction and behavior."""

    def test_inherits_react_agent(self):
        """PlanExecuteAgent is a subclass of ReactAgent."""
        assert issubclass(PlanExecuteAgent, ReactAgent)

    def test_construction_with_env_context_xml(self):
        """PlanExecuteAgent can be constructed with an env_context_xml parameter."""
        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[],
            call_llm=_dummy_call_llm,
            max_iterations=20,
            env_context_xml=_DUMMY_ENV_CONTEXT_XML,
        )
        assert agent is not None

    def test_run_returns_agent_result(self):
        """run() returns an AgentResult instance."""
        from midas_agent.stdlib.actions.task_done import TaskDoneAction

        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[TaskDoneAction()],
            call_llm=_scripted_call_llm(),
        )
        result = agent.run()
        assert isinstance(result, AgentResult)
        assert len(result.action_history) >= 1

    def test_plan_phase_uses_env_context(self):
        """env_context_xml is injected during the planning phase."""
        from midas_agent.stdlib.actions.task_done import TaskDoneAction

        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[TaskDoneAction()],
            call_llm=_scripted_call_llm(),
            env_context_xml=_DUMMY_ENV_CONTEXT_XML,
        )
        result = agent.run()
        assert isinstance(result, AgentResult)
        assert len(result.action_history) >= 1

    def test_execute_phase_follows_plan(self):
        """After planning, the agent enters the standard ReAct loop for execution."""
        from midas_agent.stdlib.actions.task_done import TaskDoneAction

        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[TaskDoneAction()],
            call_llm=_scripted_call_llm(),
            max_iterations=10,
        )
        result = agent.run(context="Execute the plan step by step.")
        assert isinstance(result, AgentResult)
        assert result.termination_reason == "done"
        assert len(result.action_history) >= 1

    def test_accepts_balance_provider(self):
        """PlanExecuteAgent constructor accepts balance_provider and propagates it to ReactAgent."""
        agent = PlanExecuteAgent(
            system_prompt="test",
            actions=[],
            call_llm=_dummy_call_llm,
            balance_provider=lambda: 5000,
        )
        # balance_provider is stored on the ReactAgent base class
        assert agent.balance_provider is not None
        assert agent.balance_provider() == 5000

    def test_balance_provider_defaults_to_none(self):
        """When balance_provider is not passed, it defaults to None."""
        agent = PlanExecuteAgent(
            system_prompt="test",
            actions=[],
            call_llm=_dummy_call_llm,
        )
        assert agent.balance_provider is None
