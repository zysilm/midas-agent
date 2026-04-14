"""Unit tests for SystemLLM — unmetered LLM call gateway.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError.
"""
import inspect

import pytest

from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hello"}], model="test-model")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSystemLLM:
    """Tests for the SystemLLM unmetered gateway."""

    def test_construction(self, fake_llm_provider):
        """SystemLLM can be constructed with an LLMProvider."""
        system_llm = SystemLLM(fake_llm_provider)
        assert system_llm is not None

    def test_call_forwards_to_provider(self, fake_llm_provider):
        """call() forwards the request to llm_provider.complete()."""
        system_llm = SystemLLM(fake_llm_provider)
        request = _make_request()
        system_llm.call(request)

        assert fake_llm_provider.call_count == 1
        logged_request, _ = fake_llm_provider.call_log[0]
        assert logged_request is request

    def test_call_returns_response(self, fake_llm_provider):
        """call() returns the LLMResponse from the provider."""
        system_llm = SystemLLM(fake_llm_provider)
        request = _make_request()
        response = system_llm.call(request)

        assert isinstance(response, LLMResponse)
        assert response.content == "test response"
        assert response.usage.input_tokens == 10
        assert response.usage.output_tokens == 5

    def test_no_training_log_interaction(self):
        """SystemLLM constructor does not accept a TrainingLog parameter.

        This verifies that SystemLLM is truly unmetered — it has no
        reference to any TrainingLog and therefore cannot record usage.
        """
        sig = inspect.signature(SystemLLM.__init__)
        param_names = [
            name for name in sig.parameters if name != "self"
        ]
        assert "training_log" not in param_names
        assert len(param_names) == 1, (
            "SystemLLM.__init__ should only accept llm_provider"
        )
