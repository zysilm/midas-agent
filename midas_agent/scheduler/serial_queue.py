"""Serial queue for TrainingLog write operations."""
from __future__ import annotations

import threading
from concurrent.futures import Future
from queue import Queue
from typing import Callable, TypeVar

T = TypeVar("T")

_SENTINEL = object()


class SerialQueue:
    """Execute callables sequentially on a single background daemon thread."""

    def __init__(self) -> None:
        self._queue: Queue = Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            future, fn = item
            try:
                result = fn()
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)

    def submit(self, callable: Callable[[], T]) -> Future[T]:
        future: Future[T] = Future()
        self._queue.put((future, callable))
        return future
