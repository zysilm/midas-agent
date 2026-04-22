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
            cool_down=0.0,
            mult_min=0.5,
            mult_max=3.0,
        )
        new_value = am.update(eviction_rate=0.5)
        assert new_value > 1.0

    def test_adaptive_multiplier_adaptive_down(self):
        """When eviction_rate == 0, the multiplier deflates via cool_down."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.2,
            cool_down=0.05,
            mult_min=0.1,
            mult_max=3.0,
        )
        new_value = am.update(eviction_rate=0.0)
        assert new_value < 1.0

    def test_adaptive_multiplier_respects_upper_bound(self):
        """Multiplier is clamped at mult_max after repeated inflation."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.0,
            cool_down=0.0,
            mult_min=0.8,
            mult_max=1.2,
        )
        # Very high eviction rate should push up, but clamp at 1.2
        for _ in range(50):
            am.update(eviction_rate=1.0)
        assert am.current_value == pytest.approx(1.2)

    def test_adaptive_multiplier_respects_lower_bound(self):
        """Multiplier is clamped at mult_min after repeated deflation.

        Uses cool_down > 0 so ER=0 actually deflates, exercising the
        lower bound clamping logic.
        """
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.2,
            cool_down=0.5,   # aggressive deflation to hit floor quickly
            mult_min=0.8,
            mult_max=3.0,
        )
        for _ in range(50):
            am.update(eviction_rate=0.0)
        assert am.current_value == pytest.approx(0.8)

    def test_adaptive_multiplier_cooldown(self):
        """cool_down is the deflation rate applied each episode when ER=0.
        multiplier *= (1 - cool_down) for each ER=0 update."""
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.2,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=3.0,
        )
        # Each ER=0 update deflates by 5%
        first = am.update(eviction_rate=0.0)
        assert first == pytest.approx(1.0 * (1 - 0.05))

        second = am.update(eviction_rate=0.0)
        assert second == pytest.approx(1.0 * (1 - 0.05) ** 2)

        third = am.update(eviction_rate=0.0)
        assert third == pytest.approx(1.0 * (1 - 0.05) ** 3)

    def test_adaptive_multiplier_dead_zone(self):
        """When 0 < ER <= er_target, multiplier does not change (dead zone).

        Design 03-05 §5.12.4: ER in (0, er_target] → no adjustment.
        """
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.2,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=3.0,
        )
        # ER exactly at er_target boundary → dead zone, no change
        result = am.update(eviction_rate=0.2)
        assert result == pytest.approx(1.0)

        # ER slightly below er_target → still dead zone
        result = am.update(eviction_rate=0.1)
        assert result == pytest.approx(1.0)

        # ER just above zero → still dead zone
        result = am.update(eviction_rate=0.05)
        assert result == pytest.approx(1.0)

    def test_adaptive_multiplier_moderate_inflate(self):
        """When er_target < ER <= 0.5, multiplier inflates by 1.2×.

        Design 03-05 §5.12.4: moderate inflation zone.
        """
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
        )
        result = am.update(eviction_rate=0.3)
        assert result == pytest.approx(1.0 * 1.2)

        result = am.update(eviction_rate=0.5)
        assert result == pytest.approx(1.0 * 1.2 * 1.2)

    def test_adaptive_multiplier_strong_inflate(self):
        """When 0.5 < ER < 1.0, multiplier inflates by 1.3×.

        Design 03-05 §5.12.4: strong inflation zone.
        """
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
        )
        result = am.update(eviction_rate=0.7)
        assert result == pytest.approx(1.0 * 1.3)

    def test_adaptive_multiplier_emergency_double(self):
        """When ER == 1.0 (all evicted), multiplier inflates by 1.5×.

        Design 03-05 §5.12.4: emergency inflation zone.
        """
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
        )
        result = am.update(eviction_rate=1.0)
        assert result == pytest.approx(1.0 * 1.5)

    def test_adaptive_multiplier_zone_transitions(self):
        """Multiplier correctly transitions across all 5 zones in sequence.

        Design 03-05 §5.12.4: full bang-bang controller behavior.
        """
        am = AdaptiveMultiplier(
            mode="adaptive",
            init_value=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
        )
        v = 1.0

        # Zone 1: ER=0 → deflate
        v *= (1 - 0.05)
        assert am.update(eviction_rate=0.0) == pytest.approx(v)

        # Zone 2: dead zone → no change
        assert am.update(eviction_rate=0.05) == pytest.approx(v)

        # Zone 3: moderate inflate
        v *= 1.2
        assert am.update(eviction_rate=0.4) == pytest.approx(v)

        # Zone 4: strong inflate
        v *= 1.3
        assert am.update(eviction_rate=0.8) == pytest.approx(v)

        # Zone 5: emergency inflate
        v *= 1.5
        assert am.update(eviction_rate=1.0) == pytest.approx(v)

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

    def test_calculate_eta_new_workspace_best(self):
        """A new workspace (not in scores) gets the best eta of survivors."""
        allocator = self._make_allocator(score_floor=0.01)
        # ws-1 and ws-2 have scores; ws-3 is new (no score)
        scores = {"ws-1": 0.8, "ws-2": 0.4}
        costs = {"ws-1": 100, "ws-2": 100, "ws-3": 100}

        etas = allocator.calculate_eta(scores, costs)

        best_eta = 0.8 / 100  # ws-1 has the best eta
        assert pytest.approx(etas["ws-3"]) == best_eta

    def test_calculate_allocation_proportional(self):
        """Allocation is proportional to eta across workspaces."""
        allocator = self._make_allocator()
        etas = {"ws-1": 0.008, "ws-2": 0.002}

        allocations = allocator.calculate_allocation(etas, last_total_consumption=10000)

        # ws-1 has 4x the eta of ws-2, so should get ~4x the budget
        assert allocations["ws-1"] > allocations["ws-2"]
        ratio = allocations["ws-1"] / allocations["ws-2"]
        assert pytest.approx(ratio, rel=0.1) == 4.0

    def test_calculate_allocation_cold_start(self):
        """First episode with empty etas uses initial_budget for all."""
        allocator = self._make_allocator(multiplier_init=1.0)
        etas: dict[str, float] = {}

        allocations = allocator.calculate_allocation(etas, last_total_consumption=0)

        # With no etas, should return empty or handle gracefully
        assert isinstance(allocations, dict)

    def test_calculate_allocation_pool_equals_last_consumption_times_multiplier(self):
        """M_pool = C_total_last_round × multiplier.

        Design 03-05: total allocation pool is based on previous episode's
        actual consumption, not a hardcoded constant."""
        allocator = self._make_allocator(multiplier_init=1.0)
        etas = {"ws-1": 0.005, "ws-2": 0.005}  # equal etas → 50/50 split

        last_consumption = 200000  # 200k tokens consumed last round
        allocations = allocator.calculate_allocation(etas, last_total_consumption=last_consumption)

        total_allocated = sum(allocations.values())
        # multiplier=1.0, so M_pool = 200000 × 1.0 = 200000
        assert pytest.approx(total_allocated, rel=0.01) == 200000

    def test_calculate_allocation_pool_scales_with_multiplier(self):
        """M_pool scales with adaptive multiplier value.

        Design 03-05: multiplier > 1.0 = expansion mode."""
        am = AdaptiveMultiplier(
            mode="adaptive", init_value=1.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        # Inflate multiplier: ER=1.0 → emergency inflate → multiplier=1.5
        am.update(eviction_rate=1.0)
        assert am.current_value == pytest.approx(1.5)

        allocator = BudgetAllocator(
            score_floor=0.01,
            multiplier_init=1.0,
            adaptive_multiplier=am,
        )
        etas = {"ws-1": 0.005, "ws-2": 0.005}

        allocations = allocator.calculate_allocation(etas, last_total_consumption=100000)

        total_allocated = sum(allocations.values())
        # M_pool = 100000 × 1.5 = 150000
        assert pytest.approx(total_allocated, rel=0.01) == 150000

    def test_calculate_allocation_not_hardcoded(self):
        """Allocation total must depend on last_total_consumption, not a constant.

        Regression test: previous implementation used hardcoded 10000."""
        allocator = self._make_allocator(multiplier_init=1.0)
        etas = {"ws-1": 0.01}

        small = allocator.calculate_allocation(etas, last_total_consumption=1000)
        large = allocator.calculate_allocation(etas, last_total_consumption=500000)

        assert large["ws-1"] > small["ws-1"] * 100, (
            "Allocation must scale with last_total_consumption, "
            f"but got small={small['ws-1']}, large={large['ws-1']}"
        )
