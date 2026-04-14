"""SystemLLM — unmetered LLM call gateway."""
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse


class SystemLLM:
    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm_provider = llm_provider

    def call(self, request: LLMRequest) -> LLMResponse:
        return self._llm_provider.complete(request)
