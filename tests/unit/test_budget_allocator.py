"""Unit tests for BudgetAllocator and AdaptiveMultiplier.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError.
"""
import pytest

from midas_agent.scheduler.budget_allocator import BudgetAllocator, AdaptiveMultiplier


# ===========================================================================
# AdaptiveMultiplier tests
# ===========================================================================

@pytest.mark.unit
class TestAdaptiveMultiplier:
    """Tests for the AdaptiveMultiplier helper."""

    def test_adaptive_multiplier_static_mode(self):
        """In static mode, update() always returns the init_value unchanged."""
        am = AdaptiveMultiplier(mode="static", init_value=1.5)
        result = am.update(eviction_rate=0.5)
        assert result == 1.5

    def test_adaptive_multiplier_adaptive_up(self):
        """When eviction_rate > er_target, the multiplier increases."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.2,
            cool_down=0,
            mult_min=0.5,
            mult_max=3.0,
        )
        new_value = am.update(eviction_rate=0.5)
        assert new_value > 1.0

    def test_adaptive_multiplier_adaptive_down(self):
        """When eviction_rate < er_target, the multiplier decreases."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.5,
            cool_down=0,
            mult_min=0.1,
            mult_max=3.0,
        )
        new_value = am.update(eviction_rate=0.1)
        assert new_value < 1.0

    def test_adaptive_multiplier_respects_bounds(self):
        """Multiplier is clamped between mult_min and mult_max."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.0,
            cool_down=0,
            mult_min=0.8,
            mult_max=1.2,
        )
        # Very high eviction rate should push up, but clamp at 1.2
        for _ in range(50):
            am.update(eviction_rate=1.0)
        assert am.current_value <= 1.2

        # Very low eviction rate should push down, but clamp at 0.8
        am2 = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=1.0,
            cool_down=0,
            mult_min=0.8,
            mult_max=1.2,
        )
        for _ in range(50):
            am2.update(eviction_rate=0.0)
        assert am2.current_value >= 0.8

    def test_adaptive_multiplier_cooldown(self):
        """After an update, the next cool_down episodes are skipped (no change)."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.2,
            cool_down=3,
            mult_min=0.5,
            mult_max=3.0,
        )
        first = am.update(eviction_rate=0.8)
        assert first != 1.0  # First update should change

        # Next 3 updates should be skipped (cooldown)
        for _ in range(3):
            val = am.update(eviction_rate=0.8)
            assert val == first, "Multiplier should not change during cooldown"

        # 4th update after cooldown should change again
        val = am.update(eviction_rate=0.8)
        assert val != first, "Multiplier should update after cooldown expires"

    def test_adaptive_multiplier_current_value(self):
        """current_value property returns the current multiplier."""
        am = AdaptiveMultiplier(mode="static", init_value=2.5)
        assert am.current_value == 2.5


# ===========================================================================
# BudgetAllocator tests
# ===========================================================================

@pytest.mark.unit
class TestBudgetAllocator:
    """Tests for the BudgetAllocator."""

    def _make_allocator(
        self,
        score_floor: float = 0.01,
        multiplier_init: float = 1.0,
    ) -> BudgetAllocator:
        am = AdaptiveMultiplier(mode="static", init_value=multiplier_init)
        return BudgetAllocator(
            score_floor=score_floor,
            multiplier_init=multiplier_init,
            adaptive_multiplier=am,
        )

    def test_construction(self):
        """BudgetAllocator can be constructed with score_floor, multiplier_init, and AdaptiveMultiplier."""
        allocator = self._make_allocator()
        assert allocator is not None

    def test_calculate_eta_formula(self):
        """eta = max(S, score_floor) / C for each workspace."""
        allocator = self._make_allocator(score_floor=0.01)
        scores = {"ws-1": 0.8, "ws-2": 0.4}
        costs = {"ws-1": 100, "ws-2": 200}

        etas = allocator.calculate_eta(scores, costs)

        assert pytest.approx(etas["ws-1"]) == 0.8 / 100
        assert pytest.approx(etas["ws-2"]) == 0.4 / 200

    def test_calculate_eta_score_floor(self):
        """When S < score_floor, score_floor is used instead."""
        allocator = self._make_allocator(score_floor=0.1)
        scores = {"ws-1": 0.05}  # below floor
        costs = {"ws-1": 100}

        etas = allocator.calculate_eta(scores, costs)

        # Should use floor (0.1) not actual score (0.05)
        assert pytest.approx(etas["ws-1"]) == 0.1 / 100

    def test_calculate_eta_new_workspace_median(self):
        """A new workspace (not in scores) gets the median eta of survivors."""
        allocator = self._make_allocator(score_floor=0.01)
        # ws-1 and ws-2 have scores; ws-3 is new (no score)
        scores = {"ws-1": 0.8, "ws-2": 0.4}
        costs = {"ws-1": 100, "ws-2": 100, "ws-3": 100}

        etas = allocator.calculate_eta(scores, costs)

        eta_1 = 0.8 / 100
        eta_2 = 0.4 / 100
        median_eta = (eta_1 + eta_2) / 2  # median of two values
        assert pytest.approx(etas["ws-3"]) == median_eta

    def test_calculate_allocation_proportional(self):
        """Allocation is proportional to eta across workspaces."""
        allocator = self._make_allocator()
        etas = {"ws-1": 0.008, "ws-2": 0.002}

        allocations = allocator.calculate_allocation(etas)

        # ws-1 has 4x the eta of ws-2, so should get ~4x the budget
        assert allocations["ws-1"] > allocations["ws-2"]
        ratio = allocations["ws-1"] / allocations["ws-2"]
        assert pytest.approx(ratio, rel=0.1) == 4.0

    def test_calculate_allocation_cold_start(self):
        """First episode with empty etas uses initial_budget for all."""
        allocator = self._make_allocator(multiplier_init=1.0)
        etas: dict[str, float] = {}

        allocations = allocator.calculate_allocation(etas)

        # With no etas, should return empty or handle gracefully
        assert isinstance(allocations, dict)
