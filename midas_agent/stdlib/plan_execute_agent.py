"""PlanExecuteAgent — Plan then Execute two-phase agent."""
from __future__ import annotations

from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import Action
from midas_agent.stdlib.react_agent import AgentResult, ReactAgent


class PlanExecuteAgent(ReactAgent):
    def __init__(
        self,
        system_prompt: str,
        actions: list[Action],
        call_llm: Callable[[LLMRequest], LLMResponse],
        max_iterations: int | None = None,
        market_info_provider: Callable[[], str] | None = None,
    ) -> None:
        raise NotImplementedError

    def run(self, context: str | None = None) -> AgentResult:
        raise NotImplementedError
