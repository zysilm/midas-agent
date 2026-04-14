"""Integration Test Suite 8: Observability.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError. These tests define expected behavior for the
Observer + TrainingLog + HookSet wiring.

Components under test:
  - Observer (hook consumer, event recording, export)
  - TrainingLog (append-only ledger)
  - HookSet (callback wiring between TrainingLog and Observer)
  - InMemoryStorageBackend (test double for persistence)

The integration pattern: create an Observer, wire its methods into a HookSet,
pass that HookSet to TrainingLog. When TrainingLog operations happen, the
hooks fire and Observer records the events.
"""
from __future__ import annotations

import os

import pytest

from midas_agent.observability.observer import Observer
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import HookSet, TrainingLog

from tests.integration.conftest import (
    InMemoryStorageBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_observer_hookset(observer: Observer) -> HookSet:
    """Wire an Observer's methods into a HookSet for TrainingLog integration."""
    return HookSet(
        on_workspace_created=lambda **kw: observer.on_workspace_created(**kw),
        on_workspace_evicted=lambda **kw: observer.on_workspace_evicted(**kw),
        on_allocate=lambda **kw: observer.on_allocate(**kw),
        on_transfer=lambda **kw: observer.on_transfer(**kw),
        on_consume=lambda **kw: observer.on_consume(**kw),
    )


def _build_observability_stack(
    output_dir: str,
) -> tuple[Observer, TrainingLog, InMemoryStorageBackend]:
    """Construct the full Observer + TrainingLog stack."""
    storage = InMemoryStorageBackend()
    observer = Observer(output_dir=output_dir)
    hooks = _make_observer_hookset(observer)
    queue = SerialQueue()
    training_log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)
    return observer, training_log, storage


class SpyObserver(Observer):
    """Observer subclass that records all calls for assertion.

    This wraps the real Observer so we can count and inspect invocations
    while still testing the full integration path.
    """

    def __init__(self, output_dir: str) -> None:
        super().__init__(output_dir=output_dir)
        self._spy_calls: dict[str, list[dict]] = {}

    def _record(self, method: str, kwargs: dict) -> None:
        self._spy_calls.setdefault(method, []).append(kwargs)

    def on_workspace_created(self, workspace_id: str, timestamp: float) -> None:
        self._record("on_workspace_created", {
            "workspace_id": workspace_id, "timestamp": timestamp,
        })
        super().on_workspace_created(workspace_id=workspace_id, timestamp=timestamp)

    def on_workspace_evicted(self, workspace_id: str, timestamp: float) -> None:
        self._record("on_workspace_evicted", {
            "workspace_id": workspace_id, "timestamp": timestamp,
        })
        super().on_workspace_evicted(workspace_id=workspace_id, timestamp=timestamp)

    def on_allocate(
        self, tx_id: str, to: str, amount: int, to_balance_after: int, timestamp: float
    ) -> None:
        self._record("on_allocate", {
            "tx_id": tx_id, "to": to, "amount": amount,
            "to_balance_after": to_balance_after, "timestamp": timestamp,
        })
        super().on_allocate(
            tx_id=tx_id, to=to, amount=amount,
            to_balance_after=to_balance_after, timestamp=timestamp,
        )

    def on_transfer(
        self,
        tx_id: str,
        from_entity: str,
        to: str,
        amount: int,
        from_balance_after: int,
        to_balance_after: int,
        timestamp: float,
    ) -> None:
        self._record("on_transfer", {
            "tx_id": tx_id, "from_entity": from_entity, "to": to,
            "amount": amount, "from_balance_after": from_balance_after,
            "to_balance_after": to_balance_after, "timestamp": timestamp,
        })
        super().on_transfer(
            tx_id=tx_id, from_entity=from_entity, to=to, amount=amount,
            from_balance_after=from_balance_after,
            to_balance_after=to_balance_after, timestamp=timestamp,
        )

    def on_consume(
        self,
        tx_id: str,
        entity_id: str,
        amount: int,
        workspace_id: str,
        balance_after: int,
        timestamp: float,
    ) -> None:
        self._record("on_consume", {
            "tx_id": tx_id, "entity_id": entity_id, "amount": amount,
            "workspace_id": workspace_id, "balance_after": balance_after,
            "timestamp": timestamp,
        })
        super().on_consume(
            tx_id=tx_id, entity_id=entity_id, amount=amount,
            workspace_id=workspace_id, balance_after=balance_after,
            timestamp=timestamp,
        )

    def get_calls(self, method: str) -> list[dict]:
        return list(self._spy_calls.get(method, []))

    def call_count(self, method: str) -> int:
        return len(self._spy_calls.get(method, []))


def _build_spy_observability_stack(
    output_dir: str,
) -> tuple[SpyObserver, TrainingLog, InMemoryStorageBackend]:
    """Construct the full SpyObserver + TrainingLog stack."""
    storage = InMemoryStorageBackend()
    observer = SpyObserver(output_dir=output_dir)
    hooks = _make_observer_hookset(observer)
    queue = SerialQueue()
    training_log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)
    return observer, training_log, storage


# ===========================================================================
# Integration tests
# ===========================================================================


@pytest.mark.integration
class TestObservabilityIntegration:
    """Suite 8: Observer + TrainingLog + HookSet wiring."""

    # -----------------------------------------------------------------------
    # IT-8.1: 3 allocations -> Observer on_allocate called 3 times
    # -----------------------------------------------------------------------

    def test_three_allocations_fire_observer_on_allocate(self, temp_dir):
        """Three allocation records should trigger Observer.on_allocate
        exactly 3 times with correct amounts and running balances."""
        output_dir = os.path.join(temp_dir, "observer_output")
        os.makedirs(output_dir, exist_ok=True)

        observer, training_log, storage = _build_spy_observability_stack(output_dir)

        training_log.record_allocate(to="ws1", amount=1000)
        training_log.record_allocate(to="ws2", amount=2000)
        training_log.record_allocate(to="ws1", amount=500)

        alloc_calls = observer.get_calls("on_allocate")
        assert len(alloc_calls) == 3, (
            f"Expected 3 on_allocate calls, got {len(alloc_calls)}"
        )

        # First allocation: ws1 gets 1000, balance = 1000
        assert alloc_calls[0]["to"] == "ws1"
        assert alloc_calls[0]["amount"] == 1000
        assert alloc_calls[0]["to_balance_after"] == 1000

        # Second allocation: ws2 gets 2000, balance = 2000
        assert alloc_calls[1]["to"] == "ws2"
        assert alloc_calls[1]["amount"] == 2000
        assert alloc_calls[1]["to_balance_after"] == 2000

        # Third allocation: ws1 gets 500 more, balance = 1500
        assert alloc_calls[2]["to"] == "ws1"
        assert alloc_calls[2]["amount"] == 500
        assert alloc_calls[2]["to_balance_after"] == 1500

    # -----------------------------------------------------------------------
    # IT-8.2: consume with entity_id != workspace_id
    # -----------------------------------------------------------------------

    def test_consume_dual_attribution_observer(self, temp_dir):
        """When a consume has both entity_id and workspace_id (Graph Emergence
        free agent pattern), Observer.on_consume must receive both values."""
        output_dir = os.path.join(temp_dir, "observer_output")
        os.makedirs(output_dir, exist_ok=True)

        observer, training_log, storage = _build_spy_observability_stack(output_dir)

        # Allocate to the agent entity (or workspace -- the budget holder)
        training_log.record_allocate(to="agent_x", amount=5000)

        # Consume with dual attribution
        training_log.record_consume(
            entity_id="agent_x", amount=200, workspace_id="ws_host"
        )

        consume_calls = observer.get_calls("on_consume")
        assert len(consume_calls) == 1

        call = consume_calls[0]
        assert call["entity_id"] == "agent_x"
        assert call["workspace_id"] == "ws_host"
        assert call["amount"] == 200
        assert call["balance_after"] == 4800  # 5000 - 200

    # -----------------------------------------------------------------------
    # IT-8.3: Budget depletion -> Observer on_workspace_evicted fires
    # -----------------------------------------------------------------------

    def test_budget_depletion_fires_eviction_observer(self, temp_dir):
        """When a consume causes the balance to drop to zero or below,
        the on_workspace_evicted hook fires and Observer records it."""
        output_dir = os.path.join(temp_dir, "observer_output")
        os.makedirs(output_dir, exist_ok=True)

        observer, training_log, storage = _build_spy_observability_stack(output_dir)

        training_log.record_allocate(to="ws_doomed", amount=300)

        # Eviction should NOT have fired yet
        assert observer.call_count("on_workspace_evicted") == 0

        # Overdraw the budget
        training_log.record_consume(entity_id="ws_doomed", amount=500)

        # Now eviction must have fired
        eviction_calls = observer.get_calls("on_workspace_evicted")
        assert len(eviction_calls) == 1
        assert eviction_calls[0]["workspace_id"] == "ws_doomed"

    # -----------------------------------------------------------------------
    # IT-8.4: Multi-episode simulation -> export_trends produces output
    # -----------------------------------------------------------------------

    def test_multi_episode_export_trends(self, temp_dir):
        """Simulate multiple episodes worth of operations. Calling
        export_trends() should produce an output file path that exists."""
        output_dir = os.path.join(temp_dir, "observer_output")
        os.makedirs(output_dir, exist_ok=True)

        observer, training_log, storage = _build_observability_stack(output_dir)

        # Episode 1: allocate and consume for 2 workspaces
        training_log.record_allocate(to="ws1", amount=5000)
        training_log.record_allocate(to="ws2", amount=3000)
        training_log.record_consume(entity_id="ws1", amount=1000)
        training_log.record_consume(entity_id="ws2", amount=800)

        # Episode 2: more allocations and consumption
        training_log.record_allocate(to="ws1", amount=4000)
        training_log.record_allocate(to="ws2", amount=2500)
        training_log.record_consume(entity_id="ws1", amount=2000)
        training_log.record_consume(entity_id="ws2", amount=1500)

        # Episode 3: introduce transfers
        training_log.record_allocate(to="ws1", amount=3000)
        training_log.record_allocate(to="ws3", amount=6000)
        training_log.record_transfer(from_entity="ws3", to="ws1", amount=1000)
        training_log.record_consume(entity_id="ws1", amount=500)
        training_log.record_consume(entity_id="ws3", amount=2000)

        # Export trends for all episodes
        output_path = observer.export_trends()

        assert isinstance(output_path, str)
        assert len(output_path) > 0
        assert os.path.exists(output_path), (
            f"export_trends() returned '{output_path}' but file does not exist"
        )

        # Verify the output file has content
        with open(output_path) as f:
            content = f.read()
        assert len(content) > 0, "Exported trends file must not be empty"

    # -----------------------------------------------------------------------
    # IT-8.5: Observer exception -> LogEntry still written, exception propagates
    # -----------------------------------------------------------------------

    def test_observer_exception_propagates_but_entry_written(self, temp_dir):
        """If Observer.on_consume raises an exception, the LogEntry must
        still be written to storage. The exception should propagate to
        the caller."""
        output_dir = os.path.join(temp_dir, "observer_output")
        os.makedirs(output_dir, exist_ok=True)

        storage = InMemoryStorageBackend()

        class ExplodingObserver(Observer):
            """Observer that raises on on_consume."""

            def on_consume(self, **kwargs) -> None:
                raise RuntimeError("observer kaboom")

        exploding_observer = ExplodingObserver(output_dir=output_dir)

        hooks = HookSet(
            on_allocate=lambda **kw: None,  # allocate works fine
            on_consume=lambda **kw: exploding_observer.on_consume(**kw),
        )
        queue = SerialQueue()
        training_log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)

        # Allocate succeeds (no explosion)
        training_log.record_allocate(to="ws_boom", amount=5000)

        # Consume should propagate the observer exception
        with pytest.raises(RuntimeError, match="observer kaboom"):
            training_log.record_consume(entity_id="ws_boom", amount=100)

        # Despite the exception, the consume entry must be persisted
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="ws_boom", type="consume")
        )
        assert len(consume_entries) == 1, (
            "LogEntry must be written to storage even if the observer hook raises"
        )
        assert consume_entries[0].amount == 100
        assert consume_entries[0].to_balance_after == 4900
