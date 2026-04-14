"""Production resource meter — budget cap without TrainingLog."""
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.scheduler.resource_meter import BudgetExhaustedError


class ProductionResourceMeter:
    """Enforces a total token budget without writing to a TrainingLog."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        budget: int,
    ) -> None:
        self._llm_provider = llm_provider
        self._budget = budget
        self._consumed = 0

    @property
    def remaining(self) -> int:
        return self._budget - self._consumed

    @property
    def consumed(self) -> int:
        return self._consumed

    def process(self, request: LLMRequest) -> LLMResponse:
        if self._consumed >= self._budget:
            raise BudgetExhaustedError(
                f"Production budget exhausted ({self._consumed}/{self._budget})"
            )

        response = self._llm_provider.complete(request)

        total = response.usage.input_tokens + response.usage.output_tokens
        self._consumed += total

        return response
