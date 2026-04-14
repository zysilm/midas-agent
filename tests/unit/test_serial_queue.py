"""Unit tests for SerialQueue."""
from concurrent.futures import Future

import pytest

from midas_agent.scheduler.serial_queue import SerialQueue


@pytest.mark.unit
class TestSerialQueue:
    """Tests for the SerialQueue single-threaded task executor."""

    def test_submit_returns_future(self):
        """submit() returns a concurrent.futures.Future."""
        queue = SerialQueue()
        future = queue.submit(lambda: 42)

        assert isinstance(future, Future)

    def test_future_contains_result(self):
        """The Future returned by submit() resolves to the callable's return value."""
        queue = SerialQueue()
        future = queue.submit(lambda: "hello")

        assert future.result(timeout=5) == "hello"

    def test_fifo_ordering(self):
        """Tasks submitted to the queue execute in FIFO order."""
        queue = SerialQueue()
        execution_order: list[int] = []

        def make_task(n: int):
            def task():
                execution_order.append(n)
                return n
            return task

        futures = [queue.submit(make_task(i)) for i in range(5)]

        # Wait for all futures to complete
        results = [f.result(timeout=5) for f in futures]

        assert results == [0, 1, 2, 3, 4]
        assert execution_order == [0, 1, 2, 3, 4]

    def test_exception_propagation(self):
        """When a submitted callable raises, the exception propagates through the Future."""
        queue = SerialQueue()

        def failing_task():
            raise ValueError("intentional test error")

        future = queue.submit(failing_task)

        with pytest.raises(ValueError, match="intentional test error"):
            future.result(timeout=5)

    def test_multiple_sequential_submits(self):
        """Submitting several tasks sequentially, all complete with correct results."""
        queue = SerialQueue()

        futures = []
        for i in range(10):
            futures.append(queue.submit(lambda x=i: x * x))

        results = [f.result(timeout=5) for f in futures]
        assert results == [i * i for i in range(10)]
