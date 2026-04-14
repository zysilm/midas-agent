"""LLM provider abstract base class."""
from abc import ABC, abstractmethod

from midas_agent.llm.types import LLMRequest, LLMResponse


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
