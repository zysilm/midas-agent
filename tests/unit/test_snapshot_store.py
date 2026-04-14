"""Unit tests for ConfigSnapshotStore and ConfigSnapshot."""
import dataclasses

import pytest

from midas_agent.workspace.config_evolution.snapshot_store import (
    ConfigSnapshot,
    ConfigSnapshotStore,
    SnapshotFilter,
)


@pytest.mark.unit
class TestConfigSnapshot:
    """Tests for the ConfigSnapshot frozen data class."""

    def test_snapshot_fields(self):
        """ConfigSnapshot stores all required fields correctly."""
        snap = ConfigSnapshot(
            episode_id="ep-1",
            workspace_id="ws-1",
            config_yaml="steps:\n  - id: s1",
            eta=0.85,
            score=0.9,
            cost=500,
            summary="First attempt, good results",
        )

        assert snap.episode_id == "ep-1"
        assert snap.workspace_id == "ws-1"
        assert snap.config_yaml == "steps:\n  - id: s1"
        assert snap.eta == 0.85
        assert snap.score == 0.9
        assert snap.cost == 500
        assert snap.summary == "First attempt, good results"

    def test_snapshot_is_frozen(self):
        """Attempting to modify a frozen ConfigSnapshot raises FrozenInstanceError."""
        snap = ConfigSnapshot(
            episode_id="ep-1",
            workspace_id="ws-1",
            config_yaml="yaml",
            eta=0.5,
            score=0.7,
            cost=100,
            summary="test",
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.score = 1.0  # type: ignore[misc]


@pytest.mark.unit
class TestConfigSnapshotStore:
    """Tests for the ConfigSnapshotStore class."""

    def _make_snapshot(self, episode_id: str = "ep-1", workspace_id: str = "ws-1", eta: float = 0.8) -> ConfigSnapshot:
        """Create a test ConfigSnapshot."""
        return ConfigSnapshot(
            episode_id=episode_id,
            workspace_id=workspace_id,
            config_yaml="steps:\n  - id: s1",
            eta=eta,
            score=0.9,
            cost=500,
            summary="test snapshot",
        )

    def test_store_construction(self):
        """ConfigSnapshotStore can be constructed with a store_dir."""
        store = ConfigSnapshotStore(store_dir="/tmp/test")

        assert store is not None

    def test_store_save(self):
        """save() stores a snapshot without error."""
        store = ConfigSnapshotStore(store_dir="/tmp/test")
        snap = self._make_snapshot()

        store.save(snap)  # Should not raise

    def test_store_query_all(self):
        """query(None) returns all saved snapshots."""
        store = ConfigSnapshotStore(store_dir="/tmp/test")
        snap1 = self._make_snapshot(episode_id="ep-1")
        snap2 = self._make_snapshot(episode_id="ep-2")

        store.save(snap1)
        store.save(snap2)

        results = store.query(None)
        assert isinstance(results, list)
        assert len(results) == 2

    def test_store_query_with_filter(self):
        """query() with a SnapshotFilter returns only matching snapshots."""
        store = ConfigSnapshotStore(store_dir="/tmp/test")
        snap1 = self._make_snapshot(episode_id="ep-1", workspace_id="ws-1")
        snap2 = self._make_snapshot(episode_id="ep-2", workspace_id="ws-2")

        store.save(snap1)
        store.save(snap2)

        filt = SnapshotFilter(workspace_id="ws-1")
        results = store.query(filt)

        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0].workspace_id == "ws-1"
