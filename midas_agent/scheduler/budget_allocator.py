"""Budget allocator and adaptive multiplier."""
from __future__ import annotations

import statistics


class AdaptiveMultiplier:
    """Bang-bang controller for the budget multiplier.

    Supports two modes:
    - "static": multiplier never changes from init_value.
    - "adaptive": 5-zone bang-bang controller adjusts multiplier based on
      eviction rate (ER).
    """

    def __init__(
        self,
        mode: str,
        init_value: float,
        er_target: float | None = None,
        cool_down: float | None = None,
        mult_min: float | None = None,
        mult_max: float | None = None,
    ) -> None:
        self.mode = mode
        self.init_value = init_value
        self.er_target = er_target
        self.cool_down = cool_down
        self.mult_min = mult_min
        self.mult_max = mult_max
        self._value = init_value

    @property
    def current_value(self) -> float:
        return self._value

    def update(self, eviction_rate: float) -> float:
        """Update multiplier based on eviction rate using a 5-zone bang-bang controller.

        Zone 1: ER == 0.0        -> deflate by (1 - cool_down)
        Zone 2: 0 < ER <= target -> dead zone, no change
        Zone 3: target < ER <= 0.5 -> moderate inflate (* 1.2)
        Zone 4: 0.5 < ER < 1.0  -> strong inflate (* 1.5)
        Zone 5: ER == 1.0       -> emergency double (* 2.0)
        """
        if self.mode == "static":
            self._value = self.init_value
            return self._value

        # Adaptive mode
        if eviction_rate == 0.0:
            # Zone 1: deflation
            self._value *= (1 - self.cool_down)
        elif eviction_rate <= self.er_target:
            # Zone 2: dead zone, no change
            pass
        elif eviction_rate <= 0.5:
            # Zone 3: moderate inflate
            self._value *= 1.2
        elif eviction_rate < 1.0:
            # Zone 4: strong inflate
            self._value *= 1.5
        else:
            # Zone 5: ER == 1.0, emergency double
            self._value *= 2.0

        # Clamp between bounds
        self._value = max(self.mult_min, min(self.mult_max, self._value))
        return self._value


class BudgetAllocator:
    """Proportional budget allocator using eta = S / C efficiency metric."""

    def __init__(
        self,
        score_floor: float,
        multiplier_init: float,
        adaptive_multiplier: AdaptiveMultiplier,
    ) -> None:
        self.score_floor = score_floor
        self.multiplier_init = multiplier_init
        self.adaptive_multiplier = adaptive_multiplier
        self._last_etas: dict[str, float] = {}

    def calculate_eta(
        self,
        workspace_scores: dict[str, float],
        workspace_costs: dict[str, int],
    ) -> dict[str, float]:
        """Compute eta = max(score, score_floor) / cost for each workspace.

        Workspaces present in costs but absent from scores (new workspaces)
        receive the median eta of the scored workspaces.
        """
        etas: dict[str, float] = {}
        scored_etas: list[float] = []

        # First pass: compute etas for workspaces that have scores
        for ws_id, cost in workspace_costs.items():
            if ws_id in workspace_scores:
                score = max(workspace_scores[ws_id], self.score_floor)
                eta = score / cost
                etas[ws_id] = eta
                scored_etas.append(eta)

        # Second pass: assign median eta to new workspaces (no score)
        if scored_etas:
            median_eta = statistics.median(scored_etas)
            for ws_id in workspace_costs:
                if ws_id not in workspace_scores:
                    etas[ws_id] = median_eta

        self._last_etas = etas
        return etas

    def calculate_allocation(
        self,
        episode_etas: dict[str, float],
    ) -> dict[str, int]:
        """Distribute budget proportionally to etas.

        Total budget = 10000 * multiplier_init * adaptive_multiplier.current_value.
        Each workspace receives total * (eta_i / sum_etas), rounded to int.

        Returns empty dict on cold start (no etas).
        """
        if not episode_etas:
            return {}

        total_budget = 10000 * self.multiplier_init * self.adaptive_multiplier.current_value
        eta_sum = sum(episode_etas.values())

        allocations: dict[str, int] = {}
        for ws_id, eta in episode_etas.items():
            allocations[ws_id] = int(total_budget * (eta / eta_sum))

        return allocations
