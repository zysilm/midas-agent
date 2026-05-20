"""Unit tests for the HTA per-episode summary builder (issue #46 Phase 1.C)."""
import json
import tempfile
from pathlib import Path

import pytest

from midas_agent.workspace.hta.analysis.episode_summary import (
    build_summary, write_summary,
)
from midas_agent.workspace.hta.graph import DecisionGraph, NodeKind, NodeStatus


def _dp(graph, decision_type, hyps, winner, escalated=False):
    """Add a decision node with hypotheses and a winner."""
    node = graph.add_node(
        NodeKind.DECISION, decision_type, status=NodeStatus.DONE,
        decision_type=decision_type, winner_hypothesis=winner,
    )
    node.payload = {
        "hypotheses": hyps,
        "winner": winner,
        "escalated": escalated,
    }
    return node


def _exec(graph, label, iterations=10, stuck=False, stuck_reason=None):
    node = graph.add_node(
        NodeKind.EXECUTION, label, status=NodeStatus.DONE,
    )
    node.payload = {
        "termination_reason": "done",
        "iterations": iterations,
        "stuck": stuck,
        "stuck_reason": stuck_reason,
    }
    return node


def _h(name, score, advantage, test_payload=""):
    return {
        "name": name,
        "rationale": f"r {name}",
        "predicted_path": f"pkg/{name}.py",
        "test_payload": test_payload,
        "score": score,
        "advantage": advantage,
    }


@pytest.mark.unit
class TestBuildSummary:
    def test_scenario_a_rcl_collapse_with_escalation_to_spec(self):
        """RCL collapsed (std=0, escalated=True); spec_interpretation fired;
        re-RCL produced a real winner; fix_locality discriminated with sentinel.
        """
        g = DecisionGraph()
        # Bootstrap node mirrors the engine — empty payload so it's excluded
        # from the execution-node iteration totals.
        g.add_node(NodeKind.EXECUTION, "bootstrap", status=NodeStatus.DONE)
        # First RCL — collapsed and escalated.
        _dp(g, "root_cause_localization", [
            _h("framework_default_value", 0.5, 0.0),
            _h("operator_overload_path", 0.5, 0.0),
            _h("serialization_roundtrip", 0.5, 0.0),
        ], winner=None, escalated=True)
        # spec_interpretation
        _dp(g, "spec_interpretation", [
            _h("literal_reading", 0.9, 1.41),
            _h("inverse_reading", 0.2, -0.71),
            _h("scope_widened", 0.0, -0.71),
        ], winner="literal_reading")
        # Re-entry RCL — real winner.
        _dp(g, "root_cause_localization", [
            _h("framework_default_value", 1.0, 1.41),
            _h("operator_overload_path", 0.0, -0.71),
            _h("__novel__:newthing", 0.0, -0.71),
        ], winner="framework_default_value")
        _exec(g, "reproduce", iterations=15)
        _dp(g, "fix_locality_scope", [
            _h("root_layer", 1.0, 1.41, test_payload="assert x, 'HTA_LAYER_HIT'"),
            _h("surface_patch", 0.2, -0.71, test_payload="print(1)"),
            _h("intermediate_layer", 0.2, -0.71, test_payload="print(2)"),
        ], winner="root_layer")
        _exec(g, "implement", iterations=8)

        s = build_summary("astropy__astropy-99999", "test-branch", g,
                          initial_budget=1_500_000, final_budget=800_000,
                          tier2_calls_used=1)

        # RCL section reflects the ACTIVE RCL (the re-entry one), not the
        # collapsed one. But escalated=True flag is carried.
        assert s["rcl"]["fired"] is True
        assert s["rcl"]["winner"] == "framework_default_value"
        assert s["rcl"]["winner_advantage"] == pytest.approx(1.41)
        assert s["rcl"]["escalated"] is True
        # The novel class appeared in the re-entry RCL -> any_tier2_call True.
        assert s["rcl"]["any_tier2_call"] is True
        assert s["rcl"]["winner_class_is_novel"] is False
        assert "framework_default_value" in s["rcl"]["classes_seen"]
        assert s["rcl"]["std_collapsed"] is False  # re-entry didn't collapse

        # Spec_interpretation fired exactly once via escalation.
        assert s["spec_interpretation"]["fired"] is True
        assert s["spec_interpretation"]["trigger"] == "rcl_escalation"

        # fix_locality — sentinel in winner.
        assert s["fix_locality"]["winner"] == "root_layer"
        assert s["fix_locality"]["sentinel_in_winner_payload"] is True
        assert s["fix_locality"]["sentinel_in_any_payload"] is True
        assert s["fix_locality"]["sentinel_count"] == 1
        assert s["fix_locality"]["std_collapsed"] is False

        # Budget
        assert s["budget"]["initial"] == 1_500_000
        assert s["budget"]["used"] == 700_000
        assert s["budget"]["fraction_used"] == pytest.approx(700_000 / 1_500_000)

        # IC didn't fire
        assert s["investigation_continuation"]["fired_count"] == 0
        assert sum(s["investigation_continuation"]["signal_breakdown"].values()) == 0

        # Decision count = 4 (RCL + spec + RCL + fix_locality)
        assert s["decision_count"] == 4
        assert s["graph_node_count"] == g._counter

    def test_scenario_b_ic_fired_via_same_file_read_5x(self):
        """An execution node was flagged stuck via same_file_read_5x;
        IC fired and chose persist_same_path. The signal_breakdown picks it up."""
        g = DecisionGraph()
        # Bootstrap node mirrors the engine — empty payload so it's excluded
        # from the execution-node iteration totals.
        g.add_node(NodeKind.EXECUTION, "bootstrap", status=NodeStatus.DONE)
        _dp(g, "root_cause_localization", [
            _h("operator_overload_path", 1.0, 1.41),
            _h("framework_default_value", 0.0, -0.71),
            _h("serialization_roundtrip", 0.0, -0.71),
        ], winner="operator_overload_path")
        _exec(g, "reproduce", iterations=22, stuck=True,
              stuck_reason="same_file_read_5x:/testbed/x.py")
        _dp(g, "investigation_continuation", [
            _h("persist_same_path", 1.0, 1.40),
            _h("pivot_target", 0.4, -0.71),
            _h("abandon", 0.3, -0.71),
        ], winner="persist_same_path")
        _exec(g, "reproduce", iterations=8, stuck=False)

        s = build_summary("django__django-99998", "test-branch", g,
                          initial_budget=1_500_000, final_budget=900_000,
                          tier2_calls_used=0)
        assert s["investigation_continuation"]["fired_count"] == 1
        assert s["investigation_continuation"]["signal_breakdown"]["same_file_read_5x"] == 1
        # No same_error_3x, no budget_60pct_no_evidence.
        assert s["investigation_continuation"]["signal_breakdown"]["same_error_3x"] == 0
        assert s["investigation_continuation"]["verdicts"] == ["persist_same_path"]
        # Stuck count = 1
        assert s["execution_nodes"]["stuck_count"] == 1
        # Total iterations across all exec nodes (bootstrap has no iterations field).
        assert s["execution_nodes"]["total_iterations"] == 22 + 8

    def test_scenario_c_no_decisions_empty_graph(self):
        """Engine never opened any decision points (budget exhausted very early)."""
        g = DecisionGraph()
        # Bootstrap node mirrors the engine — empty payload so it's excluded
        # from the execution-node iteration totals.
        g.add_node(NodeKind.EXECUTION, "bootstrap", status=NodeStatus.DONE)
        s = build_summary("astropy__astropy-99997", "test-branch", g,
                          initial_budget=1_500_000, final_budget=0,
                          tier2_calls_used=0)
        assert s["rcl"]["fired"] is False
        assert s["spec_interpretation"]["fired"] is False
        assert s["fix_locality"]["fired"] is False
        assert s["investigation_continuation"]["fired_count"] == 0
        assert s["decision_count"] == 0
        assert s["budget"]["fraction_used"] == pytest.approx(1.0)

    def test_novel_class_winner_sets_flags(self):
        g = DecisionGraph()
        # Bootstrap node mirrors the engine — empty payload so it's excluded
        # from the execution-node iteration totals.
        g.add_node(NodeKind.EXECUTION, "bootstrap", status=NodeStatus.DONE)
        _dp(g, "root_cause_localization", [
            _h("__novel__:weird", 0.7, 1.41),
            _h("framework_default_value", 0.0, -0.71),
            _h("operator_overload_path", 0.0, -0.71),
        ], winner="__novel__:weird")
        s = build_summary("x__y-1", "b", g, initial_budget=10, final_budget=5,
                          tier2_calls_used=1)
        assert s["rcl"]["winner_class_is_novel"] is True
        assert s["rcl"]["any_tier2_call"] is True


@pytest.mark.unit
class TestWriteSummary:
    def test_atomic_write_and_roundtrip(self):
        d = tempfile.mkdtemp(prefix="ep_summary_")
        s = {"issue_id": "x__y-1", "rcl": {"fired": False}}
        path = write_summary(s, d)
        assert Path(path).exists()
        # Tmp file should not linger.
        assert not Path(path + ".tmp").exists()
        with open(path) as f:
            assert json.load(f) == s

    def test_overwrites_existing(self):
        d = tempfile.mkdtemp(prefix="ep_summary_")
        write_summary({"issue_id": "x", "v": 1}, d)
        write_summary({"issue_id": "x", "v": 2}, d)
        with open(Path(d) / "analysis" / "x.json") as f:
            assert json.load(f)["v"] == 2
