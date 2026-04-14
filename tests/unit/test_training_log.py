"""Unit tests for TrainingLog, LogEntry, and HookSet."""
import dataclasses
import time
from unittest.mock import MagicMock

import pytest

from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import HookSet, LogEntry, TrainingLog


# ---------------------------------------------------------------------------
# LogEntry tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogEntry:
    """Tests for the LogEntry frozen data class."""

    def test_log_entry_fields(self):
        """LogEntry stores all fields correctly."""
        entry = LogEntry(
            tx_id="tx_001",
            type="allocate",
            from_entity=None,
            to="workspace_1",
            amount=500,
            from_balance_after=None,
            to_balance_after=500,
            workspace_id="ws_1",
            timestamp=1000.0,
        )

        assert entry.tx_id == "tx_001"
        assert entry.type == "allocate"
        assert entry.from_entity is None
        assert entry.to == "workspace_1"
        assert entry.amount == 500
        assert entry.from_balance_after is None
        assert entry.to_balance_after == 500
        assert entry.workspace_id == "ws_1"
        assert entry.timestamp == 1000.0

    def test_log_entry_is_frozen(self):
        """Attempting to modify a frozen LogEntry raises FrozenInstanceError."""
        entry = LogEntry(
            tx_id="tx_002",
            type="consume",
            from_entity=None,
            to="agent_a",
            amount=10,
            from_balance_after=None,
            to_balance_after=90,
            workspace_id="ws_1",
            timestamp=2000.0,
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.amount = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# HookSet tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHookSet:
    """Tests for the HookSet data class."""

    def test_hookset_defaults_none(self):
        """All HookSet hooks default to None."""
        hooks = HookSet()

        assert hooks.on_workspace_created is None
        assert hooks.on_workspace_evicted is None
        assert hooks.on_allocate is None
        assert hooks.on_transfer is None
        assert hooks.on_consume is None
        assert hooks.on_time_paused is None
        assert hooks.on_time_resumed is None

    def test_hookset_custom_hooks(self):
        """Setting specific hook callbacks stores them correctly."""
        on_alloc = MagicMock()
        on_consume = MagicMock()

        hooks = HookSet(on_allocate=on_alloc, on_consume=on_consume)

        assert hooks.on_allocate is on_alloc
        assert hooks.on_consume is on_consume
        assert hooks.on_transfer is None


# ---------------------------------------------------------------------------
# TrainingLog tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrainingLog:
    """Tests for the TrainingLog, which is the append-only ledger for token accounting."""

    def test_training_log_construction(self, in_memory_storage):
        """TrainingLog can be constructed with storage, hooks, and serial_queue."""
        hooks = HookSet()
        queue = SerialQueue()

        # The stub __init__ raises NotImplementedError -- this is the TDD red phase.
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        assert log is not None

    def test_record_allocate(self, in_memory_storage):
        """record_allocate returns a LogEntry with type='allocate'."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        entry = log.record_allocate(to="workspace_1", amount=1000)

        assert isinstance(entry, LogEntry)
        assert entry.type == "allocate"
        assert entry.to == "workspace_1"
        assert entry.amount == 1000

    def test_record_allocate_triggers_hook(self, in_memory_storage):
        """record_allocate invokes the on_allocate hook when set."""
        on_alloc = MagicMock()
        hooks = HookSet(on_allocate=on_alloc)
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="workspace_1", amount=500)

        on_alloc.assert_called_once()

    def test_record_consume(self, in_memory_storage):
        """record_consume returns a LogEntry with type='consume'."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        # First allocate tokens so there is a balance to consume from
        log.record_allocate(to="agent_a", amount=100)
        entry = log.record_consume(entity_id="agent_a", amount=10, workspace_id="ws_1")

        assert isinstance(entry, LogEntry)
        assert entry.type == "consume"
        assert entry.to == "agent_a"
        assert entry.amount == 10

    def test_record_consume_triggers_hook(self, in_memory_storage):
        """record_consume invokes the on_consume hook when set."""
        on_consume = MagicMock()
        hooks = HookSet(on_consume=on_consume)
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="agent_a", amount=100)
        log.record_consume(entity_id="agent_a", amount=10)

        on_consume.assert_called_once()

    def test_record_consume_eviction_on_zero_balance(self, in_memory_storage):
        """When consume brings balance to zero, on_workspace_evicted hook fires."""
        on_evict = MagicMock()
        hooks = HookSet(on_workspace_evicted=on_evict)
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="agent_a", amount=50)
        log.record_consume(entity_id="agent_a", amount=50, workspace_id="ws_1")

        on_evict.assert_called_once()

    def test_record_transfer(self, in_memory_storage):
        """record_transfer returns a LogEntry with type='transfer'."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="pool", amount=500)
        entry = log.record_transfer(from_entity="pool", to="workspace_2", amount=200)

        assert isinstance(entry, LogEntry)
        assert entry.type == "transfer"
        assert entry.from_entity == "pool"
        assert entry.to == "workspace_2"
        assert entry.amount == 200

    def test_record_transfer_insufficient_balance(self, in_memory_storage):
        """record_transfer raises when from_entity has insufficient funds."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="pool", amount=100)

        with pytest.raises(Exception):
            log.record_transfer(from_entity="pool", to="workspace_2", amount=999)

    def test_record_transfer_triggers_hook(self, in_memory_storage):
        """record_transfer invokes the on_transfer hook when set."""
        on_transfer = MagicMock()
        hooks = HookSet(on_transfer=on_transfer)
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="pool", amount=1000)
        log.record_transfer(from_entity="pool", to="workspace_3", amount=300)

        on_transfer.assert_called_once()

    def test_get_balance(self, in_memory_storage):
        """get_balance returns the net token balance for an entity."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="agent_b", amount=1000)
        log.record_consume(entity_id="agent_b", amount=200)

        balance = log.get_balance("agent_b")
        assert balance == 800

    def test_is_active(self, in_memory_storage):
        """is_active returns True when an entity has a positive balance and entries."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="agent_c", amount=500)

        assert log.is_active("agent_c") is True

    def test_get_log_entries_with_filter(self, in_memory_storage):
        """get_log_entries filters by entity_id, type, and time range."""
        hooks = HookSet()
        queue = SerialQueue()
        log = TrainingLog(storage=in_memory_storage, hooks=hooks, serial_queue=queue)

        log.record_allocate(to="agent_d", amount=300)
        log.record_allocate(to="agent_e", amount=400)
        log.record_consume(entity_id="agent_d", amount=50)

        entries = log.get_log_entries(
            LogFilter(entity_id="agent_d", type="allocate")
        )

        assert len(entries) == 1
        assert entries[0].to == "agent_d"
        assert entries[0].type == "allocate"
