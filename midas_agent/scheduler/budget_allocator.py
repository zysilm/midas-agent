"""Budget allocator and adaptive multiplier."""
from __future__ import annotations


class AdaptiveMultiplier:
    def __init__(
        self,
        mode: str,
        init_value: float,
        er_target: float | None = None,
        cool_down: float | None = None,
        mult_min: float | None = None,
        mult_max: float | None = None,
    ) -> None:
        raise NotImplementedError

    @property
    def current_value(self) -> float:
        raise NotImplementedError

    def update(self, eviction_rate: float) -> float:
        raise NotImplementedError


class BudgetAllocator:
    def __init__(
        self,
        score_floor: float,
        multiplier_init: float,
        adaptive_multiplier: AdaptiveMultiplier,
    ) -> None:
        raise NotImplementedError

    def calculate_eta(
        self,
        workspace_scores: dict[str, float],
        workspace_costs: dict[str, int],
    ) -> dict[str, float]:
        raise NotImplementedError

    def calculate_allocation(
        self,
        episode_etas: dict[str, float],
    ) -> dict[str, int]:
        raise NotImplementedError
