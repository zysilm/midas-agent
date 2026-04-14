"""Unit tests for ResourceMeter — metered LLM call gateway.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError.
"""
import pytest
from unittest.mock import MagicMock, patch

from midas_agent.scheduler.resource_meter import ResourceMeter, BudgetExhaustedError
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.training_log import TrainingLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hi"}], model="test-model")


def _make_response(input_tokens: int = 10, output_tokens: int = 5) -> LLMResponse:
    return LLMResponse(
        content="ok",
        tool_calls=None,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestResourceMeter:
    """Tests for the ResourceMeter metered LLM gateway."""

    def test_construction(self, fake_llm_provider):
        """ResourceMeter can be constructed with a TrainingLog and LLMProvider."""
        training_log = MagicMock(spec=TrainingLog)
        meter = ResourceMeter(training_log, fake_llm_provider)
        assert meter is not None

    def test_process_admit_forward_debit(self, fake_llm_provider):
        """process() follows the 3-phase flow: check balance, call LLM, record consume."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 1000
        meter = ResourceMeter(training_log, fake_llm_provider)

        request = _make_request()
        meter.process(request, entity_id="ws-1")

        # Phase 1: admit — check balance
        training_log.get_balance.assert_called_once_with("ws-1")
        # Phase 2: forward — call LLM (verified through provider)
        assert fake_llm_provider.call_count == 1
        # Phase 3: debit — record consume
        training_log.record_consume.assert_called_once()

    def test_process_raises_budget_exhausted(self, fake_llm_provider):
        """process() raises BudgetExhaustedError when the entity balance is zero or negative."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 0
        meter = ResourceMeter(training_log, fake_llm_provider)

        request = _make_request()
        with pytest.raises(BudgetExhaustedError):
            meter.process(request, entity_id="ws-1")

    def test_process_records_actual_usage(self, fake_llm_provider):
        """The debit amount equals the total token count from the LLM response."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 1000
        meter = ResourceMeter(training_log, fake_llm_provider)

        request = _make_request()
        meter.process(request, entity_id="ws-1")

        # The fake_llm_provider returns usage(10, 5) => total 15 tokens
        args, kwargs = training_log.record_consume.call_args
        consumed = kwargs.get("amount") if "amount" in kwargs else args[1]
        assert consumed == 15

    def test_process_returns_llm_response(self, fake_llm_provider):
        """process() returns the LLMResponse from the provider."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 1000
        meter = ResourceMeter(training_log, fake_llm_provider)

        request = _make_request()
        response = meter.process(request, entity_id="ws-1")

        assert isinstance(response, LLMResponse)
        assert response.content == "test response"

    def test_process_with_workspace_id(self, fake_llm_provider):
        """process() passes workspace_id for Graph Emergence cost attribution."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 1000
        meter = ResourceMeter(training_log, fake_llm_provider)

        request = _make_request()
        meter.process(request, entity_id="agent-1", workspace_id="ws-ge-1")

        # record_consume should receive workspace_id
        call_kwargs = training_log.record_consume.call_args[1]
        assert call_kwargs.get("workspace_id") == "ws-ge-1"

    def test_process_llm_error_no_debit(self):
        """If the LLM call raises an exception, no consume is recorded."""
        from tests.unit.conftest import FakeLLMProvider

        provider = FakeLLMProvider(
            responses=[_make_response()],
            errors={0: RuntimeError("LLM crashed")},
        )
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 1000
        meter = ResourceMeter(training_log, provider)

        request = _make_request()
        with pytest.raises(RuntimeError, match="LLM crashed"):
            meter.process(request, entity_id="ws-1")

        training_log.record_consume.assert_not_called()

    def test_concurrent_overdraft_allowed(self, fake_llm_provider):
        """Overdraft between admit and debit is allowed by design.

        Two concurrent calls can both pass the admit phase even if the
        combined usage exceeds the balance. This is intentional — the
        system tolerates transient overdraft.
        """
        training_log = MagicMock(spec=TrainingLog)
        # Balance is low but positive — two calls will overdraft
        training_log.get_balance.return_value = 5
        meter = ResourceMeter(training_log, fake_llm_provider)

        request = _make_request()
        # First call succeeds (balance=5 > 0)
        response = meter.process(request, entity_id="ws-1")
        assert isinstance(response, LLMResponse)
        # Verify consume was recorded even though the actual usage (15) > balance (5)
        training_log.record_consume.assert_called_once()
