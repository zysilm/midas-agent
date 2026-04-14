"""Unit tests for ProductionResourceMeter."""
import pytest

from midas_agent.inference.production_meter import ProductionResourceMeter
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.resource_meter import BudgetExhaustedError


def _make_request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hi"}], model="test")


def _make_response(input_tokens: int = 10, output_tokens: int = 5) -> LLMResponse:
    return LLMResponse(
        content="ok",
        tool_calls=None,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.mark.unit
class TestProductionResourceMeter:
    def test_process_returns_response(self, fake_llm_provider):
        meter = ProductionResourceMeter(fake_llm_provider, budget=1000)
        response = meter.process(_make_request())
        assert isinstance(response, LLMResponse)

    def test_tracks_consumption(self, fake_llm_provider):
        meter = ProductionResourceMeter(fake_llm_provider, budget=1000)
        meter.process(_make_request())
        # fake provider returns usage(10, 5) = 15 tokens
        assert meter.consumed == 15
        assert meter.remaining == 985

    def test_raises_when_budget_exhausted(self, fake_llm_provider):
        meter = ProductionResourceMeter(fake_llm_provider, budget=10)
        # First call consumes 15 tokens, exceeding budget of 10
        meter.process(_make_request())
        assert meter.consumed == 15
        # Second call should be rejected
        with pytest.raises(BudgetExhaustedError):
            meter.process(_make_request())

    def test_zero_budget_rejects_immediately(self, fake_llm_provider):
        meter = ProductionResourceMeter(fake_llm_provider, budget=0)
        with pytest.raises(BudgetExhaustedError):
            meter.process(_make_request())

    def test_multiple_calls_accumulate(self, fake_llm_provider):
        meter = ProductionResourceMeter(fake_llm_provider, budget=100)
        meter.process(_make_request())  # 15
        meter.process(_make_request())  # 15
        meter.process(_make_request())  # 15
        assert meter.consumed == 45
        assert meter.remaining == 55

    def test_llm_error_no_debit(self):
        from tests.unit.conftest import FakeLLMProvider

        provider = FakeLLMProvider(
            responses=[_make_response()],
            errors={0: RuntimeError("LLM crashed")},
        )
        meter = ProductionResourceMeter(provider, budget=1000)
        with pytest.raises(RuntimeError, match="LLM crashed"):
            meter.process(_make_request())
        assert meter.consumed == 0
