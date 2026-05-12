"""Workspace abstract base class."""
from abc import ABC, abstractmethod
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.types import Issue


class Workspace(ABC):
    def __init__(
        self,
        workspace_id: str,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self.workspace_id = workspace_id
        self.calls: list[tuple[str, dict]] = []
        self.budget_received: int = 0
        self.work_dir: str = ""
        self._last_patch: str = ""

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
    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> dict | None:
        raise NotImplementedError
