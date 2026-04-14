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
        self.agent_id = agent_id
        self.workspace_id = workspace_id
        self._system_llm = system_llm
        self._max_context_tokens = max_context_tokens
        self._messages: list[dict] = []

    @property
    def conversation_history(self) -> list[dict]:
        return list(self._messages)

    def _estimate_tokens(self) -> int:
        """Estimate total token count using rough heuristic: 4 chars per token."""
        total_chars = sum(
            len(str(m.get("content", ""))) for m in self._messages
        )
        return total_chars // 4

    def add_message(self, message: dict) -> None:
        self._messages.append(message)
        if self._estimate_tokens() > self._max_context_tokens * 0.8:
            self.compact()

    def compact(self) -> None:
        """Call system_llm to summarize conversation. Replace messages with a single summary."""
        if not self._messages:
            return
        summary_request = LLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": "Summarize the following conversation concisely.",
                },
                {
                    "role": "user",
                    "content": "\n".join(
                        f"{m.get('role', 'unknown')}: {m.get('content', '')}"
                        for m in self._messages
                    ),
                },
            ],
            model="default",
        )
        response = self._system_llm(summary_request)
        summary_content = response.content or "summary"
        self._messages = [{"role": "system", "content": summary_content}]

    def get_messages(self) -> list[dict]:
        return list(self._messages)
