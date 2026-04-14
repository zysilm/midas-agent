"""Observer — hook consumer for observability."""
from __future__ import annotations

import json
import os
import time
from typing import Any


class Observer:
    """Records observability events and exports trend data to disk."""

    def __init__(self, output_dir: str) -> None:
        self._output_dir = output_dir
        self._events: list[dict[str, Any]] = []

    def on_workspace_created(self, workspace_id: str, timestamp: float) -> None:
        self._events.append({
            "type": "workspace_created",
            "workspace_id": workspace_id,
            "timestamp": timestamp,
        })

    def on_workspace_evicted(self, workspace_id: str, timestamp: float) -> None:
        self._events.append({
            "type": "workspace_evicted",
            "workspace_id": workspace_id,
            "timestamp": timestamp,
        })

    def on_allocate(
        self, tx_id: str, to: str, amount: int, to_balance_after: int, timestamp: float
    ) -> None:
        self._events.append({
            "type": "allocate",
            "tx_id": tx_id,
            "to": to,
            "amount": amount,
            "to_balance_after": to_balance_after,
            "timestamp": timestamp,
        })

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
        self._events.append({
            "type": "transfer",
            "tx_id": tx_id,
            "from_entity": from_entity,
            "to": to,
            "amount": amount,
            "from_balance_after": from_balance_after,
            "to_balance_after": to_balance_after,
            "timestamp": timestamp,
        })

    def on_consume(
        self,
        tx_id: str,
        entity_id: str,
        amount: int,
        workspace_id: str,
        balance_after: int,
        timestamp: float,
    ) -> None:
        self._events.append({
            "type": "consume",
            "tx_id": tx_id,
            "entity_id": entity_id,
            "amount": amount,
            "workspace_id": workspace_id,
            "balance_after": balance_after,
            "timestamp": timestamp,
        })

    def on_time_paused(self, workspace_id: str, timestamp: float) -> None:
        self._events.append({
            "type": "time_paused",
            "workspace_id": workspace_id,
            "timestamp": timestamp,
        })

    def on_time_resumed(self, workspace_id: str, timestamp: float) -> None:
        self._events.append({
            "type": "time_resumed",
            "workspace_id": workspace_id,
            "timestamp": timestamp,
        })

    def print_episode_summary(self, episode_id: str) -> None:
        event_count = len(self._events)
        print(f"Episode {episode_id} summary: {event_count} events recorded")

    def print_live_status(self) -> None:
        event_count = len(self._events)
        print(f"Observer live status: {event_count} events recorded")

    def export_trends(
        self, episode_range: tuple[int, int] | None = None
    ) -> str:
        data = {
            "exported_at": time.time(),
            "episode_range": list(episode_range) if episode_range else None,
            "events": list(self._events),
        }
        file_path = os.path.join(self._output_dir, "trends.json")
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)
        return file_path
