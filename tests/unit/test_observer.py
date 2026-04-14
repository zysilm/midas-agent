"""Unit tests for Observer."""
import tempfile
import time

import pytest

from midas_agent.observability.observer import Observer


@pytest.mark.unit
class TestObserver:
    """Tests for the Observer hook consumer for observability."""

    def test_construction(self):
        """Observer accepts an output_dir parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)

            assert observer is not None

    def test_on_workspace_created(self):
        """on_workspace_created records a workspace creation event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            # Must not raise; returns None
            result = observer.on_workspace_created(
                workspace_id="ws-001",
                timestamp=ts,
            )

            assert result is None

    def test_on_workspace_evicted(self):
        """on_workspace_evicted records a workspace eviction event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            result = observer.on_workspace_evicted(
                workspace_id="ws-001",
                timestamp=ts,
            )

            assert result is None

    def test_on_allocate(self):
        """on_allocate records a budget allocation event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            result = observer.on_allocate(
                tx_id="tx-001",
                to="ws-001",
                amount=1000,
                to_balance_after=5000,
                timestamp=ts,
            )

            assert result is None

    def test_on_transfer(self):
        """on_transfer records a budget transfer event between entities."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            result = observer.on_transfer(
                tx_id="tx-002",
                from_entity="ws-001",
                to="ws-002",
                amount=500,
                from_balance_after=4500,
                to_balance_after=2500,
                timestamp=ts,
            )

            assert result is None

    def test_on_consume(self):
        """on_consume records a token consumption event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            result = observer.on_consume(
                tx_id="tx-003",
                entity_id="agent-a",
                amount=150,
                workspace_id="ws-001",
                balance_after=4350,
                timestamp=ts,
            )

            assert result is None

    def test_on_time_paused(self):
        """on_time_paused records a workspace time-pause event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            result = observer.on_time_paused(
                workspace_id="ws-001",
                timestamp=ts,
            )

            assert result is None

    def test_on_time_resumed(self):
        """on_time_resumed records a workspace time-resume event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)
            ts = time.time()

            result = observer.on_time_resumed(
                workspace_id="ws-001",
                timestamp=ts,
            )

            assert result is None

    def test_print_episode_summary(self):
        """print_episode_summary outputs a summary for a given episode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)

            # Must not raise; returns None
            result = observer.print_episode_summary(episode_id="ep-010")

            assert result is None

    def test_print_live_status(self):
        """print_live_status outputs the current live status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)

            # Must not raise; returns None
            result = observer.print_live_status()

            assert result is None

    def test_export_trends(self):
        """export_trends returns a file path string for the exported trends."""
        with tempfile.TemporaryDirectory() as tmpdir:
            observer = Observer(output_dir=tmpdir)

            result = observer.export_trends()

            assert isinstance(result, str)
