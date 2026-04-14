"""Training log — append-only source of truth for token accounting."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter, StorageBackend


@dataclass(frozen=True)
class LogEntry:
    tx_id: str
    type: str  # "allocate" | "transfer" | "consume"
    from_entity: str | None
    to: str
    amount: int
    from_balance_after: int | None
    to_balance_after: int
    workspace_id: str | None
    timestamp: float


@dataclass
class HookSet:
    on_workspace_created: Callable | None = None
    on_workspace_evicted: Callable | None = None
    on_allocate: Callable | None = None
    on_transfer: Callable | None = None
    on_consume: Callable | None = None
    on_time_paused: Callable | None = None
    on_time_resumed: Callable | None = None


class TrainingLog:
    def __init__(
        self,
        storage: StorageBackend,
        hooks: HookSet,
        serial_queue: SerialQueue,
    ) -> None:
        self._storage = storage
        self._hooks = hooks
        self._serial_queue = serial_queue
        self._balances: dict[str, int] = {}
        self._tx_counter: int = 0
        self._lock = threading.Lock()

        # Crash recovery: rebuild balances from existing storage entries.
        existing = self._storage.query(LogFilter())
        for entry in existing:
            self._tx_counter += 1
            if entry.type == "allocate":
                self._balances[entry.to] = self._balances.get(entry.to, 0) + entry.amount
            elif entry.type == "consume":
                self._balances[entry.to] = self._balances.get(entry.to, 0) - entry.amount
            elif entry.type == "transfer":
                if entry.from_entity is not None:
                    self._balances[entry.from_entity] = (
                        self._balances.get(entry.from_entity, 0) - entry.amount
                    )
                self._balances[entry.to] = self._balances.get(entry.to, 0) + entry.amount

    def _next_tx_id(self) -> str:
        tx_id = f"tx_{self._tx_counter:06d}"
        self._tx_counter += 1
        return tx_id

    def get_balance(self, entity_id: str) -> int:
        with self._lock:
            return self._balances.get(entity_id, 0)

    def is_active(self, entity_id: str) -> bool:
        with self._lock:
            return entity_id in self._balances and self._balances[entity_id] > 0

    def record_allocate(self, to: str, amount: int) -> LogEntry:
        with self._lock:
            tx_id = self._next_tx_id()
            timestamp = time.time()
            self._balances[to] = self._balances.get(to, 0) + amount
            to_balance_after = self._balances[to]

        entry = LogEntry(
            tx_id=tx_id,
            type="allocate",
            from_entity="system",
            to=to,
            amount=amount,
            from_balance_after=None,
            to_balance_after=to_balance_after,
            workspace_id=None,
            timestamp=timestamp,
        )

        self._serial_queue.submit(lambda: self._storage.append(entry)).result()

        if self._hooks.on_allocate is not None:
            self._hooks.on_allocate(
                tx_id=tx_id,
                to=to,
                amount=amount,
                to_balance_after=to_balance_after,
                timestamp=timestamp,
            )

        return entry

    def record_transfer(self, from_entity: str, to: str, amount: int) -> LogEntry:
        with self._lock:
            from_balance = self._balances.get(from_entity, 0)
            if from_balance < amount:
                raise ValueError(
                    f"Insufficient balance for {from_entity}: "
                    f"has {from_balance}, needs {amount}"
                )
            tx_id = self._next_tx_id()
            timestamp = time.time()
            self._balances[from_entity] = from_balance - amount
            self._balances[to] = self._balances.get(to, 0) + amount
            from_balance_after = self._balances[from_entity]
            to_balance_after = self._balances[to]

        entry = LogEntry(
            tx_id=tx_id,
            type="transfer",
            from_entity=from_entity,
            to=to,
            amount=amount,
            from_balance_after=from_balance_after,
            to_balance_after=to_balance_after,
            workspace_id=None,
            timestamp=timestamp,
        )

        self._serial_queue.submit(lambda: self._storage.append(entry)).result()

        if self._hooks.on_transfer is not None:
            self._hooks.on_transfer(
                tx_id=tx_id,
                from_entity=from_entity,
                to=to,
                amount=amount,
                from_balance_after=from_balance_after,
                to_balance_after=to_balance_after,
                timestamp=timestamp,
            )

        return entry

    def record_consume(
        self,
        entity_id: str,
        amount: int,
        workspace_id: str | None = None,
    ) -> LogEntry:
        with self._lock:
            tx_id = self._next_tx_id()
            timestamp = time.time()
            self._balances[entity_id] = self._balances.get(entity_id, 0) - amount
            to_balance_after = self._balances[entity_id]

        entry = LogEntry(
            tx_id=tx_id,
            type="consume",
            from_entity=None,
            to=entity_id,
            amount=amount,
            from_balance_after=None,
            to_balance_after=to_balance_after,
            workspace_id=workspace_id,
            timestamp=timestamp,
        )

        # CRITICAL (IT-8.5): persist BEFORE firing hooks so the entry
        # survives even if the hook raises an exception.
        self._serial_queue.submit(lambda: self._storage.append(entry)).result()

        # Resolve the workspace_id to pass to hooks: fall back to entity_id
        # when no explicit workspace_id was provided.
        effective_workspace_id = workspace_id if workspace_id is not None else entity_id

        if self._hooks.on_consume is not None:
            self._hooks.on_consume(
                tx_id=tx_id,
                entity_id=entity_id,
                amount=amount,
                workspace_id=effective_workspace_id,
                balance_after=to_balance_after,
                timestamp=timestamp,
            )

        # Eviction fires only when the balance crosses from positive to
        # non-positive (i.e., the entity was genuinely depleted).  A free
        # agent that never had budget (balance was already <= 0 before
        # this consume) must NOT trigger eviction.
        #
        # When workspace_id differs from entity_id (Graph Emergence free
        # agent pattern) and the consume causes an overdraft (balance goes
        # negative, not merely to zero), eviction is suppressed because
        # the free agent's debt is absorbed by the hosting workspace.
        previous_balance = to_balance_after + amount
        is_free_agent_overdraft = (
            workspace_id is not None
            and workspace_id != entity_id
            and to_balance_after < 0
        )
        if (
            to_balance_after <= 0
            and previous_balance > 0
            and not is_free_agent_overdraft
            and self._hooks.on_workspace_evicted is not None
        ):
            self._hooks.on_workspace_evicted(
                workspace_id=effective_workspace_id,
                timestamp=timestamp,
            )

        return entry

    def get_log_entries(self, filter: LogFilter) -> list[LogEntry]:
        return self._storage.query(filter)
