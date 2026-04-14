"""Training log — append-only source of truth for token accounting."""
from __future__ import annotations

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
        raise NotImplementedError

    def get_balance(self, entity_id: str) -> int:
        raise NotImplementedError

    def is_active(self, entity_id: str) -> bool:
        raise NotImplementedError

    def record_allocate(self, to: str, amount: int) -> LogEntry:
        raise NotImplementedError

    def record_transfer(self, from_entity: str, to: str, amount: int) -> LogEntry:
        raise NotImplementedError

    def record_consume(
        self,
        entity_id: str,
        amount: int,
        workspace_id: str | None = None,
    ) -> LogEntry:
        raise NotImplementedError

    def get_log_entries(self, filter: LogFilter) -> list[LogEntry]:
        raise NotImplementedError
