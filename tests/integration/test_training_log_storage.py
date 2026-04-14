"""Integration Test Suite 1: TrainingLog + Storage + SerialQueue + Hooks.

All production code is NotImplementedError stubs. These tests define the
expected behavior for TDD and will pass once the production implementations
are filled in.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future

import pytest

from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import HookSet, LogEntry, TrainingLog
from tests.integration.conftest import (
    DeterministicClock,
    InMemoryStorageBackend,
    SpyHookSet,
)


# ---------------------------------------------------------------------------
# Helper: build a TrainingLog wired to the given collaborators
# ---------------------------------------------------------------------------


def _make_log(
    storage: InMemoryStorageBackend,
    hooks: HookSet | None = None,
    serial_queue: SerialQueue | None = None,
) -> TrainingLog:
    return TrainingLog(
        storage=storage,
        hooks=hooks or HookSet(),
        serial_queue=serial_queue or SerialQueue(),
    )


# ---------------------------------------------------------------------------
# IT-1.1: Balance derivation across mixed operations
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBalanceDerivation:
    """Balance is derived correctly from an interleaved sequence of
    allocations and consumptions, including overdraft (negative balance)."""

    def test_balance_after_mixed_operations(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)

        log.record_allocate(to="ws1", amount=10_000)
        log.record_consume(entity_id="ws1", amount=3_000)
        log.record_allocate(to="ws1", amount=5_000)
        log.record_consume(entity_id="ws1", amount=8_000)
        log.record_consume(entity_id="ws1", amount=5_000)

        assert log.get_balance("ws1") == -1_000
        assert log.is_active("ws1") is False


# ---------------------------------------------------------------------------
# IT-1.2: Concurrent writes via SerialQueue maintain consistency
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestConcurrentWriteConsistency:
    """Multiple threads that concurrently record_consume must produce a
    consistent, serialised ledger thanks to the SerialQueue."""

    def test_concurrent_consume_threads(
        self, in_memory_storage, spy_hooks
    ):
        queue = SerialQueue()
        log = _make_log(in_memory_storage, hooks=spy_hooks, serial_queue=queue)

        initial_budget = 10_000
        log.record_allocate(to="ws1", amount=initial_budget)

        num_threads = 10
        consume_per_thread = 100
        barrier = threading.Barrier(num_threads)
        results: list[LogEntry] = [None] * num_threads  # type: ignore[list-item]

        def worker(idx: int) -> None:
            barrier.wait()
            results[idx] = log.record_consume(
                entity_id="ws1", amount=consume_per_thread
            )

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Balance must reflect exactly 10 * 100 consumed
        expected_balance = initial_budget - (num_threads * consume_per_thread)
        assert log.get_balance("ws1") == expected_balance

        # Exactly 10 consume entries (plus the initial allocate)
        consume_entries = log.get_log_entries(
            LogFilter(entity_id="ws1", type="consume")
        )
        assert len(consume_entries) == num_threads

        # All tx_ids unique
        tx_ids = [e.tx_id for e in consume_entries]
        assert len(set(tx_ids)) == num_threads

        # tx_ids are monotonically increasing (lexicographic or numeric,
        # depending on implementation -- we just check strict ordering of
        # the sorted-by-timestamp sequence).
        timestamps = [e.timestamp for e in consume_entries]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# IT-1.3: Transfer atomicity (Graph Emergence)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTransferAtomicity:
    """A transfer between two entities atomically debits one and credits the
    other.  The returned LogEntry records both post-transfer balances."""

    def test_transfer_balances_and_log_entry(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)

        log.record_allocate(to="a", amount=5_000)
        log.record_allocate(to="b", amount=2_000)
        entry = log.record_transfer(from_entity="a", to="b", amount=3_000)

        assert log.get_balance("a") == 2_000
        assert log.get_balance("b") == 5_000

        assert entry.type == "transfer"
        assert entry.from_entity == "a"
        assert entry.to == "b"
        assert entry.amount == 3_000
        assert entry.from_balance_after == 2_000
        assert entry.to_balance_after == 5_000


# ---------------------------------------------------------------------------
# IT-1.4: Transfer insufficient balance rejection
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTransferInsufficientBalance:
    """Transferring more than the sender owns must raise an error and leave
    the ledger unchanged."""

    def test_transfer_rejected_on_insufficient_funds(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)

        log.record_allocate(to="a", amount=1_000)

        with pytest.raises(Exception):
            log.record_transfer(from_entity="a", to="b", amount=2_000)

        # No transfer entry recorded
        transfer_entries = log.get_log_entries(LogFilter(type="transfer"))
        assert len(transfer_entries) == 0

        # Sender balance unchanged
        assert log.get_balance("a") == 1_000


# ---------------------------------------------------------------------------
# IT-1.5: Hook invocation ordering and completeness
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestHookInvocationOrdering:
    """Hooks fire in operation order, carrying the expected payload."""

    def test_allocate_and_consume_hooks(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)

        log.record_allocate(to="ws1", amount=5_000)
        log.record_consume(entity_id="ws1", amount=2_000)

        # on_allocate fired once
        spy_hooks.assert_called("on_allocate", times=1)
        alloc_calls = spy_hooks.get_calls("on_allocate")
        assert alloc_calls[0]["to_balance_after"] == 5_000

        # on_consume fired once
        spy_hooks.assert_called("on_consume", times=1)
        consume_calls = spy_hooks.get_calls("on_consume")
        # After consuming 2000 from 5000, remaining balance is 3000
        assert consume_calls[0]["balance_after"] == 3_000

        # Order: allocate happened before consume (index-wise in the ledger)
        all_entries = log.get_log_entries(LogFilter(entity_id="ws1"))
        types_in_order = [e.type for e in all_entries]
        assert types_in_order == ["allocate", "consume"]


# ---------------------------------------------------------------------------
# IT-1.6: on_workspace_evicted fires on balance depletion
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEvictionHookOnDepletion:
    """on_workspace_evicted fires when a consume causes the balance to drop
    to zero or below, but NOT for prior operations with a positive balance."""

    def test_eviction_fires_on_depletion(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)

        log.record_allocate(to="ws1", amount=500)
        # Balance is 500 -- eviction should NOT have fired yet
        spy_hooks.assert_not_called("on_workspace_evicted")

        log.record_consume(entity_id="ws1", amount=600)
        # Balance is now -100 -- eviction MUST fire
        spy_hooks.assert_called("on_workspace_evicted", times=1)


# ---------------------------------------------------------------------------
# IT-1.7: Dual attribution in consume records
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDualAttribution:
    """A consume entry with both entity_id and workspace_id is queryable
    via either dimension."""

    def test_consume_queryable_by_entity_and_workspace(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)

        log.record_allocate(to="agent_x", amount=1_000)
        log.record_consume(
            entity_id="agent_x", amount=500, workspace_id="ws_2"
        )

        # Query by entity_id
        by_entity = log.get_log_entries(
            LogFilter(entity_id="agent_x", type="consume")
        )
        assert len(by_entity) == 1
        assert by_entity[0].workspace_id == "ws_2"

        # Query by workspace_id
        by_workspace = log.get_log_entries(
            LogFilter(workspace_id="ws_2", type="consume")
        )
        assert len(by_workspace) == 1
        assert by_workspace[0].to == "agent_x"

        # Both queries return the same entry
        assert by_entity[0].tx_id == by_workspace[0].tx_id


# ---------------------------------------------------------------------------
# IT-1.8: Storage backend parity (InMemory only -- placeholder)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStorageBackendParity:
    """Running an identical operation sequence against two InMemoryStorageBackend
    instances must produce identical query results.  This is a placeholder for
    future parity tests across SQLite / file backends."""

    def test_identical_sequences_yield_identical_results(self):
        storage_a = InMemoryStorageBackend()
        storage_b = InMemoryStorageBackend()

        for storage in (storage_a, storage_b):
            log = _make_log(storage)
            log.record_allocate(to="ws1", amount=5_000)
            log.record_consume(entity_id="ws1", amount=1_000)
            log.record_allocate(to="ws2", amount=3_000)
            log.record_transfer(from_entity="ws2", to="ws1", amount=500)
            log.record_consume(entity_id="ws2", amount=200, workspace_id="ws_x")

        # Compare full unfiltered queries
        results_a = storage_a.query(LogFilter())
        results_b = storage_b.query(LogFilter())
        assert len(results_a) == len(results_b)

        for ea, eb in zip(results_a, results_b):
            assert ea.type == eb.type
            assert ea.from_entity == eb.from_entity
            assert ea.to == eb.to
            assert ea.amount == eb.amount
            assert ea.from_balance_after == eb.from_balance_after
            assert ea.to_balance_after == eb.to_balance_after
            assert ea.workspace_id == eb.workspace_id

        # Filtered queries also match
        for filt in (
            LogFilter(entity_id="ws1"),
            LogFilter(type="consume"),
            LogFilter(workspace_id="ws_x"),
        ):
            qa = storage_a.query(filt)
            qb = storage_b.query(filt)
            assert len(qa) == len(qb)
            for ea, eb in zip(qa, qb):
                assert ea.type == eb.type
                assert ea.to == eb.to
                assert ea.amount == eb.amount


# ---------------------------------------------------------------------------
# IT-1.9: get_log_entries filtering combinations
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLogEntryFiltering:
    """Exercise every LogFilter field individually and in combination."""

    @pytest.fixture()
    def populated_log(self, in_memory_storage, deterministic_clock):
        """Create a training log pre-populated with diverse entries."""
        log = _make_log(in_memory_storage)

        # t=1000: allocate ws1 1000
        log.record_allocate(to="ws1", amount=1_000)
        # t=1001: allocate ws2 2000
        log.record_allocate(to="ws2", amount=2_000)
        # t=1002: consume ws1 300 (workspace_id=proj_a)
        log.record_consume(entity_id="ws1", amount=300, workspace_id="proj_a")
        # t=1003: consume ws2 400 (workspace_id=proj_b)
        log.record_consume(entity_id="ws2", amount=400, workspace_id="proj_b")
        # t=1004: transfer ws2 -> ws1 500
        log.record_transfer(from_entity="ws2", to="ws1", amount=500)
        # t=1005: consume ws1 100 (workspace_id=proj_a)
        log.record_consume(entity_id="ws1", amount=100, workspace_id="proj_a")

        return log

    def test_filter_by_entity_id(self, populated_log):
        entries = populated_log.get_log_entries(LogFilter(entity_id="ws1"))
        # ws1 appears in: allocate(to=ws1), consume(to=ws1), transfer(to=ws1),
        # consume(to=ws1)
        assert len(entries) == 4
        assert all(
            e.to == "ws1" or e.from_entity == "ws1" for e in entries
        )

    def test_filter_by_type(self, populated_log):
        entries = populated_log.get_log_entries(LogFilter(type="consume"))
        assert len(entries) == 3
        assert all(e.type == "consume" for e in entries)

    def test_filter_by_workspace_id(self, populated_log):
        entries = populated_log.get_log_entries(
            LogFilter(workspace_id="proj_a")
        )
        assert len(entries) == 2
        assert all(e.workspace_id == "proj_a" for e in entries)

    def test_filter_by_time_range(self, populated_log):
        # Grab all entries, derive a mid-range timestamp window
        all_entries = populated_log.get_log_entries(LogFilter())
        assert len(all_entries) == 6

        # Since and until are inclusive; pick the window covering the 3rd
        # through 5th entries (indices 2..4).
        t_since = all_entries[2].timestamp
        t_until = all_entries[4].timestamp
        entries = populated_log.get_log_entries(
            LogFilter(since=t_since, until=t_until)
        )
        assert len(entries) == 3
        for e in entries:
            assert t_since <= e.timestamp <= t_until

    def test_filter_intersection_entity_and_type(self, populated_log):
        entries = populated_log.get_log_entries(
            LogFilter(entity_id="ws1", type="consume")
        )
        assert len(entries) == 2
        assert all(e.type == "consume" for e in entries)
        assert all(e.to == "ws1" for e in entries)

    def test_filter_intersection_workspace_and_type(self, populated_log):
        entries = populated_log.get_log_entries(
            LogFilter(workspace_id="proj_b", type="consume")
        )
        assert len(entries) == 1
        assert entries[0].to == "ws2"
        assert entries[0].workspace_id == "proj_b"

    def test_filter_no_match_returns_empty(self, populated_log):
        entries = populated_log.get_log_entries(
            LogFilter(entity_id="nonexistent")
        )
        assert entries == []


# ---------------------------------------------------------------------------
# IT-1.10: Crash recovery simulation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCrashRecoverySimulation:
    """Re-instantiating TrainingLog over the same StorageBackend must recover
    all balances from the persisted log entries.  With InMemoryStorageBackend
    this validates that the log re-derives state from storage on construction."""

    def test_balances_intact_after_reinstantiation(
        self, in_memory_storage, spy_hooks
    ):
        log = _make_log(in_memory_storage, hooks=spy_hooks)
        log.record_allocate(to="ws1", amount=5_000)
        log.record_consume(entity_id="ws1", amount=1_200)
        log.record_allocate(to="ws2", amount=3_000)
        log.record_transfer(from_entity="ws2", to="ws1", amount=800)

        expected_ws1 = 5_000 - 1_200 + 800  # 4600
        expected_ws2 = 3_000 - 800  # 2200
        assert log.get_balance("ws1") == expected_ws1
        assert log.get_balance("ws2") == expected_ws2

        # Simulate crash: new TrainingLog, same storage backend
        log2 = _make_log(in_memory_storage, hooks=SpyHookSet())

        assert log2.get_balance("ws1") == expected_ws1
        assert log2.get_balance("ws2") == expected_ws2
        assert log2.is_active("ws1") is True
        assert log2.is_active("ws2") is True

    def test_log_entries_survive_reinstantiation(
        self, in_memory_storage
    ):
        log = _make_log(in_memory_storage)
        log.record_allocate(to="ws1", amount=2_000)
        log.record_consume(entity_id="ws1", amount=500)

        original_entries = log.get_log_entries(LogFilter(entity_id="ws1"))

        log2 = _make_log(in_memory_storage)
        recovered_entries = log2.get_log_entries(LogFilter(entity_id="ws1"))

        assert len(recovered_entries) == len(original_entries)
        for orig, recov in zip(original_entries, recovered_entries):
            assert orig.tx_id == recov.tx_id
            assert orig.type == recov.type
            assert orig.amount == recov.amount
            assert orig.to_balance_after == recov.to_balance_after
