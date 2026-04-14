"""Storage backend abstract base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from midas_agent.scheduler.training_log import LogEntry


@dataclass
class LogFilter:
    entity_id: str | None = None
    workspace_id: str | None = None
    type: str | None = None
    since: float | None = None
    until: float | None = None


class StorageBackend(ABC):
    @abstractmethod
    def append(self, entry: LogEntry) -> None:
        raise NotImplementedError

    @abstractmethod
    def query(self, filter: LogFilter) -> list[LogEntry]:
        raise NotImplementedError


class InMemoryStorageBackend(StorageBackend):
    """Simple in-memory storage backend."""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    def append(self, entry: LogEntry) -> None:
        self._entries.append(entry)

    def query(self, filter: LogFilter) -> list[LogEntry]:
        results = list(self._entries)
        if filter.entity_id is not None:
            results = [
                e
                for e in results
                if e.to == filter.entity_id or e.from_entity == filter.entity_id
            ]
        if filter.type is not None:
            results = [e for e in results if e.type == filter.type]
        if filter.workspace_id is not None:
            results = [e for e in results if e.workspace_id == filter.workspace_id]
        if filter.since is not None:
            results = [e for e in results if e.timestamp >= filter.since]
        if filter.until is not None:
            results = [e for e in results if e.timestamp <= filter.until]
        return sorted(results, key=lambda e: e.timestamp)
