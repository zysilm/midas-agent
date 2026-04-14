"""Integration Test Suite 2: ResourceMeter + TrainingLog.

TDD red phase: all tests define expected behavior for production stubs
that currently raise NotImplementedError.

These tests exercise the full admit-forward-debit pipeline with real
TrainingLog / InMemoryStorageBackend wiring (no mocks on the hot path).
"""

from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.resource_meter import BudgetExhaustedError, ResourceMeter
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import TrainingLog

from tests.integration.conftest import (
    FakeLLMProvider,
    InMemoryStorageBackend,
    SpyHookSet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(content: str = "hello") -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": content}],
        model="test-model",
    )


def _make_response(
    content: str = "ok",
    input_tokens: int = 200,
    output_tokens: int = 300,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=None,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _build_stack(
    responses: list[LLMResponse],
    errors: dict[int, Exception] | None = None,
    delay: float = 0.0,
) -> tuple[ResourceMeter, TrainingLog, FakeLLMProvider, SpyHookSet, InMemoryStorageBackend]:
    """Construct the full ResourceMeter + TrainingLog stack with test doubles."""
    storage = InMemoryStorageBackend()
    hooks = SpyHookSet()
    queue = SerialQueue()
    training_log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)
    provider = FakeLLMProvider(responses=responses, errors=errors, delay=delay)
    meter = ResourceMeter(training_log=training_log, llm_provider=provider)
    return meter, training_log, provider, hooks, storage


# ---------------------------------------------------------------------------
# IT-2.1  Happy path -- admit, forward, debit
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT21HappyPath:
    """Allocate tokens, process a request, verify balance deduction and log entry."""

    def test_admit_forward_debit(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[_make_response(input_tokens=200, output_tokens=300)],
        )

        # Allocate 10000 tokens to ws1
        training_log.record_allocate(to="ws1", amount=10000)
        assert training_log.get_balance("ws1") == 10000

        # Process a request on behalf of ws1
        request = _make_request()
        response = meter.process(request, entity_id="ws1")

        # The LLM was called exactly once
        assert provider.call_count == 1

        # Response is forwarded correctly
        assert isinstance(response, LLMResponse)
        assert response.content == "ok"
        assert response.usage.input_tokens == 200
        assert response.usage.output_tokens == 300

        # Balance is debited by total usage: 200 + 300 = 500
        assert training_log.get_balance("ws1") == 9500

        # Exactly one consume LogEntry recorded
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="ws1", type="consume")
        )
        assert len(consume_entries) == 1
        assert consume_entries[0].amount == 500
        assert consume_entries[0].to == "ws1"
        assert consume_entries[0].type == "consume"


# ---------------------------------------------------------------------------
# IT-2.2  Admission rejection (BudgetExhaustedError)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT22AdmissionRejection:
    """Zero-balance entity is rejected before the LLM is invoked."""

    def test_budget_exhausted_raises(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[_make_response()],
        )

        # Entity has no allocation -- balance is 0
        assert training_log.get_balance("ws_empty") == 0

        request = _make_request()
        with pytest.raises(BudgetExhaustedError):
            meter.process(request, entity_id="ws_empty")

        # LLM must NOT have been called
        assert provider.call_count == 0

        # No consume LogEntry created
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="ws_empty", type="consume")
        )
        assert len(consume_entries) == 0


# ---------------------------------------------------------------------------
# IT-2.3  Free agent bypass -- no admission check
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT23FreeAgentBypass:
    """A Graph Emergence free agent (balance <= 0) still gets forwarded.

    When workspace_id is provided (Graph Emergence mode), the admission
    check is against the workspace, not the agent. A free agent with no
    personal budget should still be able to make calls charged to its
    workspace, and no eviction hook should fire.
    """

    def test_free_agent_bypass(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[_make_response(input_tokens=100, output_tokens=100)],
        )

        # Workspace ws_host has budget; free agent agent_free has none
        training_log.record_allocate(to="ws_host", amount=5000)
        # agent_free deliberately has no allocation (balance = 0)

        request = _make_request()
        response = meter.process(
            request, entity_id="agent_free", workspace_id="ws_host"
        )

        # LLM was invoked
        assert provider.call_count == 1
        assert isinstance(response, LLMResponse)

        # Consume recorded
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="agent_free", type="consume")
        )
        assert len(consume_entries) == 1
        assert consume_entries[0].amount == 200  # 100 + 100

        # on_workspace_evicted must NOT fire
        hooks.assert_not_called("on_workspace_evicted")


# ---------------------------------------------------------------------------
# IT-2.4  Workspace-bound agent depletion triggers eviction
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT24DepletionEviction:
    """Overdraft on a workspace-bound entity fires the eviction hook.

    After depletion, subsequent process() calls are rejected.
    """

    def test_depletion_triggers_eviction(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[
                _make_response(input_tokens=300, output_tokens=300),  # usage = 600
                _make_response(input_tokens=100, output_tokens=100),  # never reached
            ],
        )

        # Allocate only 500 tokens
        training_log.record_allocate(to="ws_tight", amount=500)

        request = _make_request()
        # First call: usage=600, overdrafts the 500-token budget
        response = meter.process(request, entity_id="ws_tight")
        assert isinstance(response, LLMResponse)

        # Eviction hook must have fired
        hooks.assert_called("on_workspace_evicted", times=1)

        # Balance should be negative (500 - 600 = -100)
        assert training_log.get_balance("ws_tight") < 0

        # Subsequent call must be rejected
        with pytest.raises(BudgetExhaustedError):
            meter.process(_make_request(), entity_id="ws_tight")

        # LLM should only have been called once (the first time)
        assert provider.call_count == 1


# ---------------------------------------------------------------------------
# IT-2.5  Dual attribution for Graph Emergence consume
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT25DualAttribution:
    """A consume LogEntry carries both entity_id and workspace_id.

    The entry must be queryable by either dimension.
    """

    def test_dual_attribution(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[_make_response(input_tokens=50, output_tokens=50)],
        )

        # Set up workspace budget
        training_log.record_allocate(to="ws_2", amount=5000)

        request = _make_request()
        meter.process(request, entity_id="agent_x", workspace_id="ws_2")

        # Query by entity_id
        by_entity = training_log.get_log_entries(
            LogFilter(entity_id="agent_x", type="consume")
        )
        assert len(by_entity) == 1
        assert by_entity[0].to == "agent_x"
        assert by_entity[0].workspace_id == "ws_2"
        assert by_entity[0].amount == 100  # 50 + 50

        # Query by workspace_id
        by_workspace = training_log.get_log_entries(
            LogFilter(workspace_id="ws_2", type="consume")
        )
        assert len(by_workspace) == 1
        assert by_workspace[0].to == "agent_x"
        assert by_workspace[0].workspace_id == "ws_2"


# ---------------------------------------------------------------------------
# IT-2.6  Concurrent overdraft window
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT26ConcurrentOverdraft:
    """Two concurrent calls both pass admission, causing overdraft.

    This is by-design: the system tolerates transient negative balances
    rather than serialising every LLM call through a single lock.
    """

    def test_concurrent_overdraft(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[
                _make_response(input_tokens=400, output_tokens=300),  # usage = 700
                _make_response(input_tokens=400, output_tokens=300),  # usage = 700
            ],
            delay=0.05,  # small delay to widen the race window
        )

        # Allocate 1000 -- enough for one call (700) but not two (1400)
        training_log.record_allocate(to="ws_race", amount=1000)

        request_a = _make_request("request_a")
        request_b = _make_request("request_b")

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(
                meter.process, request_a, "ws_race"
            )
            future_b = executor.submit(
                meter.process, request_b, "ws_race"
            )

            response_a = future_a.result(timeout=5)
            response_b = future_b.result(timeout=5)

        # Both calls must have succeeded (no BudgetExhaustedError)
        assert isinstance(response_a, LLMResponse)
        assert isinstance(response_b, LLMResponse)

        # LLM was called twice
        assert provider.call_count == 2

        # Final balance: 1000 - 700 - 700 = -400
        assert training_log.get_balance("ws_race") == -400

        # Two consume records
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="ws_race", type="consume")
        )
        assert len(consume_entries) == 2
        assert all(e.amount == 700 for e in consume_entries)


# ---------------------------------------------------------------------------
# IT-2.7  LLM provider error propagation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT27LLMErrorPropagation:
    """When the LLM provider raises, the error propagates and no debit occurs."""

    def test_llm_error_no_debit(self):
        meter, training_log, provider, hooks, storage = _build_stack(
            responses=[_make_response()],
            errors={0: RuntimeError("upstream LLM failure")},
        )

        training_log.record_allocate(to="ws_err", amount=5000)

        request = _make_request()
        with pytest.raises(RuntimeError, match="upstream LLM failure"):
            meter.process(request, entity_id="ws_err")

        # Balance must remain unchanged
        assert training_log.get_balance("ws_err") == 5000

        # No consume LogEntry recorded
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="ws_err", type="consume")
        )
        assert len(consume_entries) == 0
