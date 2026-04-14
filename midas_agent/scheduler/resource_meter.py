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
        self._training_log = training_log
        self._llm_provider = llm_provider

    def process(
        self,
        request: LLMRequest,
        entity_id: str,
        workspace_id: str | None = None,
    ) -> LLMResponse:
        # Phase 1 — Admit: check balance
        check_id = workspace_id if workspace_id is not None else entity_id
        if self._training_log.get_balance(check_id) <= 0:
            raise BudgetExhaustedError(
                f"Budget exhausted for {check_id}"
            )

        # Phase 2 — Forward: call LLM (propagate errors without debiting)
        response = self._llm_provider.complete(request)

        # Phase 3 — Debit: record token consumption
        total = response.usage.input_tokens + response.usage.output_tokens
        effective_workspace_id = workspace_id if workspace_id is not None else entity_id
        self._training_log.record_consume(
            entity_id=entity_id,
            amount=total,
            workspace_id=effective_workspace_id,
        )

        return response
