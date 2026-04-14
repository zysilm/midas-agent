"""Integration Test Suite 3: BudgetAllocator + AdaptiveMultiplier + SelectionEngine.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError. These tests define expected behavior for the
three-component budget-selection pipeline.
"""
from __future__ import annotations

import statistics
from collections import Counter

import pytest

from midas_agent.scheduler.budget_allocator import AdaptiveMultiplier, BudgetAllocator
from midas_agent.scheduler.selection import SelectionEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_allocator(
    score_floor: float = 0.01,
    multiplier_init: float = 1.0,
    *,
    mult_mode: str = "static",
    er_target: float | None = None,
    cool_down: float | None = None,
    mult_min: float | None = None,
    mult_max: float | None = None,
) -> tuple[BudgetAllocator, AdaptiveMultiplier]:
    """Build a BudgetAllocator with its AdaptiveMultiplier and return both."""
    am = AdaptiveMultiplier(
        mode=mult_mode,
        init_value=multiplier_init,
        er_target=er_target,
        cool_down=cool_down,
        mult_min=mult_min,
        mult_max=mult_max,
    )
    allocator = BudgetAllocator(
        score_floor=score_floor,
        multiplier_init=multiplier_init,
        adaptive_multiplier=am,
    )
    return allocator, am


# ===========================================================================
# Integration tests
# ===========================================================================


@pytest.mark.integration
class TestBudgetSelectionIntegration:
    """Suite 3: BudgetAllocator + AdaptiveMultiplier + SelectionEngine."""

    # -----------------------------------------------------------------------
    # IT-3.1: Cold start allocation
    # -----------------------------------------------------------------------

    def test_cold_start_allocation(self):
        """No prior etas (empty dict). calculate_allocation returns uniform allocation."""
        allocator, _ = _make_allocator()
        allocations = allocator.calculate_allocation({})

        assert isinstance(allocations, dict)
        # With no etas at all, the result should either be empty or all
        # values equal (uniform). The key invariant: no workspace is
        # favored over another.
        if allocations:
            values = list(allocations.values())
            assert all(v == values[0] for v in values), (
                "Cold-start allocation must be uniform"
            )

    # -----------------------------------------------------------------------
    # IT-3.2: Eta-proportional allocation
    # -----------------------------------------------------------------------

    def test_eta_proportional_allocation(self):
        """scores={ws1:0.8, ws2:0.2}, costs={ws1:1000, ws2:1000}.
        Etas should be in 4:1 ratio, and allocation in 4:1 ratio."""
        allocator, _ = _make_allocator(score_floor=0.01)
        scores = {"ws1": 0.8, "ws2": 0.2}
        costs = {"ws1": 1000, "ws2": 1000}

        etas = allocator.calculate_eta(scores, costs)

        # eta = S / C  =>  ws1: 0.0008, ws2: 0.0002
        assert pytest.approx(etas["ws1"], rel=1e-6) == 0.8 / 1000
        assert pytest.approx(etas["ws2"], rel=1e-6) == 0.2 / 1000
        assert pytest.approx(etas["ws1"] / etas["ws2"], rel=1e-6) == 4.0

        allocations = allocator.calculate_allocation(etas)

        assert allocations["ws1"] > 0
        assert allocations["ws2"] > 0
        ratio = allocations["ws1"] / allocations["ws2"]
        assert pytest.approx(ratio, rel=0.1) == 4.0

    # -----------------------------------------------------------------------
    # IT-3.3: Score floor prevents zero eta
    # -----------------------------------------------------------------------

    def test_score_floor_prevents_zero_eta(self):
        """scores={ws1:0.0, ws2:1.0}, costs each 500, floor=0.01.
        ws1 eta = floor/C = 0.01/500 = 0.00002, gets nonzero allocation."""
        allocator, _ = _make_allocator(score_floor=0.01)
        scores = {"ws1": 0.0, "ws2": 1.0}
        costs = {"ws1": 500, "ws2": 500}

        etas = allocator.calculate_eta(scores, costs)

        # ws1 should use floor instead of 0.0
        assert etas["ws1"] == pytest.approx(0.01 / 500, rel=1e-6)
        assert etas["ws1"] > 0, "Score floor must prevent zero eta"

        allocations = allocator.calculate_allocation(etas)
        assert allocations["ws1"] > 0, "Workspace with floored score must get nonzero allocation"

    # -----------------------------------------------------------------------
    # IT-3.4: New workspace receives median eta
    # -----------------------------------------------------------------------

    def test_new_workspace_receives_median_eta(self):
        """3 existing etas {ws1:0.005, ws2:0.010, ws3:0.015}. New ws4 has
        no eta. ws4 receives median=0.010 for allocation."""
        allocator, _ = _make_allocator(score_floor=0.01)
        existing_etas = {"ws1": 0.005, "ws2": 0.010, "ws3": 0.015}

        # ws4 is new: it appears in costs but not in scores, so
        # calculate_eta should assign the median of the existing etas.
        scores = {"ws1": 0.005 * 100, "ws2": 0.010 * 100, "ws3": 0.015 * 100}
        costs = {"ws1": 100, "ws2": 100, "ws3": 100, "ws4": 100}

        etas = allocator.calculate_eta(scores, costs)

        median_existing = statistics.median([0.005, 0.010, 0.015])
        assert pytest.approx(etas["ws4"], rel=1e-6) == median_existing

        allocations = allocator.calculate_allocation(etas)
        assert allocations["ws4"] > 0, "New workspace must receive budget via median eta"

    # -----------------------------------------------------------------------
    # IT-3.5: SelectionEngine bottom-n eviction
    # -----------------------------------------------------------------------

    def test_selection_engine_bottom_n_eviction(self):
        """etas={ws1:0.1, ws2:0.5, ws3:0.3, ws4:0.2}, n_evict=1.
        evicted=[ws1], survivors=[ws2, ws3, ws4]."""
        engine = SelectionEngine("config_evolution", n_evict=1)
        etas = {"ws1": 0.1, "ws2": 0.5, "ws3": 0.3, "ws4": 0.2}

        evicted, survivors = engine.run_selection(etas)

        assert evicted == ["ws1"]
        assert set(survivors) == {"ws2", "ws3", "ws4"}

    # -----------------------------------------------------------------------
    # IT-3.6: Tie-breaking is random
    # -----------------------------------------------------------------------

    def test_tie_breaking_is_random(self):
        """All etas=0.5, n_evict=1. Run 100 times. All IDs appear at
        least once in the evicted set."""
        engine = SelectionEngine("config_evolution", n_evict=1)
        etas = {"ws1": 0.5, "ws2": 0.5, "ws3": 0.5, "ws4": 0.5}

        evicted_counter: Counter[str] = Counter()
        for _ in range(100):
            evicted, survivors = engine.run_selection(etas)
            assert len(evicted) == 1
            assert len(survivors) == 3
            evicted_counter[evicted[0]] += 1

        # Every workspace must have been evicted at least once across 100 runs.
        for ws_id in etas:
            assert evicted_counter[ws_id] >= 1, (
                f"{ws_id} was never evicted in 100 runs despite identical etas; "
                f"tie-breaking must be random. Counts: {dict(evicted_counter)}"
            )

    # -----------------------------------------------------------------------
    # IT-3.7: n_evict clamping
    # -----------------------------------------------------------------------

    def test_n_evict_clamping(self):
        """2 workspaces, n_evict=5. Actual eviction = min(5, 2-1) = 1.
        One survives."""
        engine = SelectionEngine("config_evolution", n_evict=5)
        etas = {"ws1": 0.3, "ws2": 0.7}

        evicted, survivors = engine.run_selection(etas)

        assert len(evicted) == 1, "Must clamp eviction to N-1"
        assert len(survivors) == 1, "At least one workspace must survive"

    # -----------------------------------------------------------------------
    # IT-3.8: Graph Emergence skips eviction
    # -----------------------------------------------------------------------

    def test_graph_emergence_skips_eviction(self):
        """runtime_mode='graph_emergence'. evicted=[], survivors=all."""
        engine = SelectionEngine("graph_emergence", n_evict=2)
        etas = {"ws1": 0.1, "ws2": 0.2, "ws3": 0.3}

        evicted, survivors = engine.run_selection(etas)

        assert evicted == []
        assert set(survivors) == {"ws1", "ws2", "ws3"}

    # -----------------------------------------------------------------------
    # IT-3.9: AdaptiveMultiplier static mode
    # -----------------------------------------------------------------------

    def test_adaptive_multiplier_static_mode(self):
        """mode='static', init=1.5. update(0.5) multiple times.
        current_value always 1.5."""
        am = AdaptiveMultiplier(mode="static", init_value=1.5)
        assert am.current_value == 1.5

        for er in [0.0, 0.1, 0.5, 0.9, 1.0]:
            result = am.update(eviction_rate=er)
            assert result == 1.5, (
                f"Static multiplier must not change; got {result} after ER={er}"
            )
            assert am.current_value == 1.5

    # -----------------------------------------------------------------------
    # IT-3.10: AdaptiveMultiplier adaptive mode -- ER tiers
    # -----------------------------------------------------------------------

    def test_adaptive_multiplier_er_tiers(self):
        """Start mult=1.0, er_target=0.1, cool_down=0.05, mult_min=0.5,
        mult_max=5.0. Verify the four ER tiers and clamping."""
        # Tier 1: ER=0.0 (below target, zero eviction) => mult *= (1 - cool_down)
        am1 = AdaptiveMultiplier(
            mode="adaptive", init_value=1.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am1.update(eviction_rate=0.0)
        assert pytest.approx(result, rel=1e-6) == 1.0 * (1 - 0.05)
        assert pytest.approx(am1.current_value, rel=1e-6) == 0.95

        # Tier 2: ER=0.05 (dead zone near target) => no change
        am2 = AdaptiveMultiplier(
            mode="adaptive", init_value=1.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am2.update(eviction_rate=0.05)
        assert pytest.approx(result, rel=1e-6) == 1.0, (
            "Dead-zone ER should leave multiplier unchanged"
        )

        # Tier 3: ER=0.3 (moderately above target) => mult *= 1.2
        am3 = AdaptiveMultiplier(
            mode="adaptive", init_value=1.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am3.update(eviction_rate=0.3)
        assert pytest.approx(result, rel=1e-6) == 1.0 * 1.2

        # Tier 4: ER=0.6 (high) => mult *= 1.5
        am4 = AdaptiveMultiplier(
            mode="adaptive", init_value=1.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am4.update(eviction_rate=0.6)
        assert pytest.approx(result, rel=1e-6) == 1.0 * 1.5

        # Tier 5: ER=1.0 (maximum) => mult *= 2.0
        am5 = AdaptiveMultiplier(
            mode="adaptive", init_value=1.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am5.update(eviction_rate=1.0)
        assert pytest.approx(result, rel=1e-6) == 1.0 * 2.0

        # Clamping at mult_max: push above 5.0
        am_high = AdaptiveMultiplier(
            mode="adaptive", init_value=4.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am_high.update(eviction_rate=1.0)  # 4.0 * 2.0 = 8.0
        assert result == 5.0, "Must clamp at mult_max"
        assert am_high.current_value == 5.0

        # Clamping at mult_min: push below 0.5
        am_low = AdaptiveMultiplier(
            mode="adaptive", init_value=0.51,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        result = am_low.update(eviction_rate=0.0)  # 0.51 * 0.95 = 0.4845
        assert result == 0.5, "Must clamp at mult_min"
        assert am_low.current_value == 0.5

    # -----------------------------------------------------------------------
    # IT-3.11: Dead zone
    # -----------------------------------------------------------------------

    def test_dead_zone(self):
        """er_target=0.1, update(er=0.05). Multiplier unchanged."""
        am = AdaptiveMultiplier(
            mode="adaptive", init_value=2.0,
            er_target=0.1, cool_down=0.05, mult_min=0.5, mult_max=5.0,
        )
        original = am.current_value
        result = am.update(eviction_rate=0.05)

        assert result == original, "Dead-zone ER must not change multiplier"
        assert am.current_value == original

    # -----------------------------------------------------------------------
    # IT-3.12: Full 3-episode simulation
    # -----------------------------------------------------------------------

    def test_full_three_episode_simulation(self):
        """Episode 1: cold start. Episode 2: compute etas, allocate, evict 1,
        multiplier updates. Episode 3: new workspace gets median eta.
        Verify consistency across the full pipeline."""
        allocator, am = _make_allocator(
            score_floor=0.01,
            multiplier_init=1.0,
            mult_mode="adaptive",
            er_target=0.2,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
        )
        engine = SelectionEngine("config_evolution", n_evict=1)

        # ===== Episode 1: Cold start =====
        cold_allocations = allocator.calculate_allocation({})
        assert isinstance(cold_allocations, dict)

        # ===== Episode 2: Etas from episode 1 scores =====
        scores_ep2 = {"ws1": 0.9, "ws2": 0.3, "ws3": 0.1}
        costs_ep2 = {"ws1": 1000, "ws2": 1000, "ws3": 1000}

        etas_ep2 = allocator.calculate_eta(scores_ep2, costs_ep2)
        assert len(etas_ep2) == 3
        # ws1 should have highest eta, ws3 lowest
        assert etas_ep2["ws1"] > etas_ep2["ws2"] > etas_ep2["ws3"]

        allocations_ep2 = allocator.calculate_allocation(etas_ep2)
        assert allocations_ep2["ws1"] > allocations_ep2["ws3"], (
            "Higher eta must yield higher allocation"
        )

        # Evict bottom 1
        evicted_ep2, survivors_ep2 = engine.run_selection(etas_ep2)
        assert evicted_ep2 == ["ws3"], "ws3 has the lowest eta"
        assert set(survivors_ep2) == {"ws1", "ws2"}

        # Update multiplier with eviction rate = 1 evicted / 3 total = 0.333
        eviction_rate = len(evicted_ep2) / len(etas_ep2)
        new_mult = am.update(eviction_rate=eviction_rate)
        assert new_mult > 1.0, (
            "ER=0.333 is above target=0.2, multiplier should increase"
        )

        # ===== Episode 3: Surviving workspaces + new ws4 =====
        scores_ep3 = {"ws1": 0.7, "ws2": 0.5}
        costs_ep3 = {"ws1": 1000, "ws2": 1000, "ws4": 1000}

        etas_ep3 = allocator.calculate_eta(scores_ep3, costs_ep3)
        assert len(etas_ep3) == 3

        # ws4 is new (not in scores). It should receive the median of the
        # existing workspace etas.
        eta_ws1 = etas_ep3["ws1"]
        eta_ws2 = etas_ep3["ws2"]
        median_eta = statistics.median([eta_ws1, eta_ws2])
        assert pytest.approx(etas_ep3["ws4"], rel=1e-6) == median_eta, (
            "New workspace must receive median eta of survivors"
        )

        allocations_ep3 = allocator.calculate_allocation(etas_ep3)
        assert all(v > 0 for v in allocations_ep3.values()), (
            "All surviving and new workspaces must receive positive allocation"
        )

        # Evict bottom 1 from the 3 remaining workspaces
        evicted_ep3, survivors_ep3 = engine.run_selection(etas_ep3)
        assert len(evicted_ep3) == 1
        assert len(survivors_ep3) == 2
        # The evicted workspace should be the one with the lowest eta
        min_eta_ws = min(etas_ep3, key=etas_ep3.get)
        assert evicted_ep3[0] == min_eta_ws
