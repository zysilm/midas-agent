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
