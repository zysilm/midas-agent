"""SystemLLM — unmetered LLM call gateway."""
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse


class SystemLLM:
    def __init__(self, llm_provider: LLMProvider) -> None:
        raise NotImplementedError

    def call(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
