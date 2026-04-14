"""Session — workspace-isolated conversation context."""
from __future__ import annotations

from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse


class Session:
    def __init__(
        self,
        agent_id: str,
        workspace_id: str,
        system_llm: Callable[[LLMRequest], LLMResponse],
        max_context_tokens: int,
    ) -> None:
        raise NotImplementedError

    @property
    def conversation_history(self) -> list[dict]:
        raise NotImplementedError

    def add_message(self, message: dict) -> None:
        raise NotImplementedError

    def compact(self) -> None:
        raise NotImplementedError

    def get_messages(self) -> list[dict]:
        raise NotImplementedError
