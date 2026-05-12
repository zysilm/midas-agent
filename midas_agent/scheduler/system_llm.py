"""SystemLLM — unmetered LLM call gateway."""
from llm_agent_toolkit.llm.provider import LLMProvider
from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse


class SystemLLM:
    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm_provider = llm_provider

    def call(self, request: LLMRequest) -> LLMResponse:
        return self._llm_provider.complete(request)
