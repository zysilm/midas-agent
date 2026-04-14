"""Config snapshot store — append-only configuration history."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigSnapshot:
    episode_id: str
    workspace_id: str
    config_yaml: str
    eta: float
    score: float
    cost: int
    summary: str


@dataclass
class SnapshotFilter:
    workspace_id: str | None = None
    episode_id: str | None = None
    min_eta: float | None = None
    top_k: int | None = None


class ConfigSnapshotStore:
    def __init__(self, store_dir: str) -> None:
        self.store_dir = store_dir
        self._snapshots: list[ConfigSnapshot] = []

    def save(self, snapshot: ConfigSnapshot) -> None:
        self._snapshots.append(snapshot)

    def query(
        self,
        filter: SnapshotFilter | None = None,
    ) -> list[ConfigSnapshot]:
        results = list(self._snapshots)

        if filter is not None:
            if filter.workspace_id is not None:
                results = [s for s in results if s.workspace_id == filter.workspace_id]
            if filter.episode_id is not None:
                results = [s for s in results if s.episode_id == filter.episode_id]
            if filter.min_eta is not None:
                results = [s for s in results if s.eta >= filter.min_eta]
            # Always sort by eta descending before applying top_k
            results.sort(key=lambda s: s.eta, reverse=True)
            if filter.top_k is not None:
                results = results[:filter.top_k]

        return results
