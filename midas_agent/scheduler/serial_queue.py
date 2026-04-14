"""Serial queue for TrainingLog write operations."""
from __future__ import annotations

from concurrent.futures import Future
from typing import Callable, TypeVar

T = TypeVar("T")


class SerialQueue:
    def submit(self, callable: Callable[[], T]) -> Future[T]:
        raise NotImplementedError
