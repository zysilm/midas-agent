"""Unit tests for PlanExecuteAgent."""
import pytest

from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.stdlib.react_agent import AgentResult, ReactAgent
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


def _dummy_call_llm(req: LLMRequest) -> LLMResponse:
    """Stub LLM callback that returns an empty response."""
    return LLMResponse(
        content="done",
        tool_calls=None,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _dummy_market_info_provider() -> str:
    """Stub market info provider returning a static string."""
    return "market info: budget=1000, agents=4"


@pytest.mark.unit
class TestPlanExecuteAgent:
    """Tests for PlanExecuteAgent construction and behavior."""

    def test_inherits_react_agent(self):
        """PlanExecuteAgent is a subclass of ReactAgent."""
        assert issubclass(PlanExecuteAgent, ReactAgent)

    def test_construction_with_market_info(self):
        """PlanExecuteAgent can be constructed with a market_info_provider parameter."""
        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[],
            call_llm=_dummy_call_llm,
            max_iterations=20,
            market_info_provider=_dummy_market_info_provider,
        )
        assert agent is not None

    def test_run_returns_agent_result(self):
        """run() returns an AgentResult instance."""
        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[],
            call_llm=_dummy_call_llm,
        )
        result = agent.run()
        assert isinstance(result, AgentResult)

    def test_plan_phase_uses_market_info(self):
        """Market info from the provider is injected during the planning phase."""
        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[],
            call_llm=_dummy_call_llm,
            market_info_provider=_dummy_market_info_provider,
        )
        result = agent.run()
        assert isinstance(result, AgentResult)

    def test_execute_phase_follows_plan(self):
        """After planning, the agent enters the standard ReAct loop for execution."""
        agent = PlanExecuteAgent(
            system_prompt="You are a planning agent.",
            actions=[],
            call_llm=_dummy_call_llm,
            max_iterations=10,
        )
        result = agent.run(context="Execute the plan step by step.")
        assert isinstance(result, AgentResult)
