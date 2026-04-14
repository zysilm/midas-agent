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
        raise NotImplementedError

    def save(self, snapshot: ConfigSnapshot) -> None:
        raise NotImplementedError

    def query(
        self,
        filter: SnapshotFilter | None = None,
    ) -> list[ConfigSnapshot]:
        raise NotImplementedError
