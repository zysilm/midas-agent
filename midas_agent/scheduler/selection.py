"""Selection engine — bottom-n eviction logic."""
from __future__ import annotations

import random


class SelectionEngine:
    """Bottom-n eviction based on workspace eta values.

    Supports two runtime modes:
    - "config_evolution": evict the n lowest-eta workspaces (at least 1 survives).
    - "graph_emergence": no eviction; all workspaces survive.
    """

    def __init__(self, runtime_mode: str, n_evict: int) -> None:
        self.runtime_mode = runtime_mode
        self.n_evict = n_evict

    def run_selection(
        self,
        workspace_etas: dict[str, float],
    ) -> tuple[list[str], list[str]]:
        """Select workspaces for eviction and survival.

        Returns:
            (evicted, survivors) -- two lists of workspace IDs.
        """
        if self.runtime_mode == "graph_emergence":
            return [], list(workspace_etas.keys())

        # config_evolution mode: bottom-n eviction
        actual_evict = min(self.n_evict, len(workspace_etas) - 1)

        # Shuffle first then stable-sort by eta so ties are broken randomly
        items = list(workspace_etas.items())
        random.shuffle(items)
        items.sort(key=lambda x: x[1])

        evicted = [ws_id for ws_id, _ in items[:actual_evict]]
        survivors = [ws_id for ws_id, _ in items[actual_evict:]]

        return evicted, survivors
