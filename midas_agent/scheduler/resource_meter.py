"""Resource meter — metered LLM call gateway."""
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.scheduler.training_log import TrainingLog


class BudgetExhaustedError(Exception):
    pass


class ResourceMeter:
    def __init__(
        self,
        training_log: TrainingLog,
        llm_provider: LLMProvider,
    ) -> None:
        raise NotImplementedError

    def process(
        self,
        request: LLMRequest,
        entity_id: str,
        workspace_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError
