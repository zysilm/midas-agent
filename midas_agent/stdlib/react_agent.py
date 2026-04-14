"""ReactAgent — ReAct loop implementation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import Action


@dataclass
class ActionRecord:
    action_name: str
    arguments: dict
    result: str
    timestamp: float


@dataclass
class AgentResult:
    output: str
    iterations: int
    termination_reason: str  # "done" | "budget_exhausted" | "max_iterations" | "no_action"
    action_history: list[ActionRecord] = field(default_factory=list)


class ReactAgent:
    def __init__(
        self,
        system_prompt: str,
        actions: list[Action],
        call_llm: Callable[[LLMRequest], LLMResponse],
        max_iterations: int | None = None,
    ) -> None:
        raise NotImplementedError

    def run(self, context: str | None = None) -> AgentResult:
        raise NotImplementedError
