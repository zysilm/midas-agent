"""Bottom-n workspace eviction by eta.

STATUS: Currently dormant. Under all shipped configs, workspace_count=1
and n_evict=0, which means run_selection() always returns ([], all_workspaces).
This module is preserved for potential future use by multi-candidate
architectures (see HTA design proposal). Do not enable n_evict > 0
without also wiring up replace_evicted() in the active training entry
point — see scheduler.py:Scheduler.replace_evicted().
"""
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
