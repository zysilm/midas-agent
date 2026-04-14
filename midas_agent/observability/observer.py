"""Observer — hook consumer for observability."""
from __future__ import annotations


class Observer:
    def __init__(self, output_dir: str) -> None:
        raise NotImplementedError

    def on_workspace_created(self, workspace_id: str, timestamp: float) -> None:
        raise NotImplementedError

    def on_workspace_evicted(self, workspace_id: str, timestamp: float) -> None:
        raise NotImplementedError

    def on_allocate(
        self, tx_id: str, to: str, amount: int, to_balance_after: int, timestamp: float
    ) -> None:
        raise NotImplementedError

    def on_transfer(
        self,
        tx_id: str,
        from_entity: str,
        to: str,
        amount: int,
        from_balance_after: int,
        to_balance_after: int,
        timestamp: float,
    ) -> None:
        raise NotImplementedError

    def on_consume(
        self,
        tx_id: str,
        entity_id: str,
        amount: int,
        workspace_id: str,
        balance_after: int,
        timestamp: float,
    ) -> None:
        raise NotImplementedError

    def on_time_paused(self, workspace_id: str, timestamp: float) -> None:
        raise NotImplementedError

    def on_time_resumed(self, workspace_id: str, timestamp: float) -> None:
        raise NotImplementedError

    def print_episode_summary(self, episode_id: str) -> None:
        raise NotImplementedError

    def print_live_status(self) -> None:
        raise NotImplementedError

    def export_trends(
        self, episode_range: tuple[int, int] | None = None
    ) -> str:
        raise NotImplementedError
