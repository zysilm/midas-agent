"""Workspace abstract base class."""
from abc import ABC, abstractmethod
from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.types import Issue


class Workspace(ABC):
    def __init__(
        self,
        workspace_id: str,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self.workspace_id = workspace_id

    @abstractmethod
    def receive_budget(self, amount: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def execute(self, issue: Issue) -> None:
        raise NotImplementedError

    @abstractmethod
    def submit_patch(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def post_episode(self, eval_results: dict) -> dict | None:
        raise NotImplementedError
