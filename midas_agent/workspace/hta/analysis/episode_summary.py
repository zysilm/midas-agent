"""Build the per-episode HTA evaluation summary (issue #46 Phase 1.C).

Walks a completed DecisionGraph and emits a stable JSON summary the
post-run analyzer can consume without re-parsing every graph. The
schema matches the contract in issue #46 — fields may be added but
existing fields must not change shape without bumping a version.

The write is atomic (``.tmp`` + ``os.replace``) and is intentionally
wrapped by the caller in ``try/except`` — a failed summary write must
never abort the episode.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from pathlib import Path
from typing import Any

from midas_agent.workspace.hta.decision_point import NOVEL_PREFIX
from midas_agent.workspace.hta.graph import DecisionGraph, NodeKind

logger = logging.getLogger(__name__)

_LAYER_HIT_SENTINEL = "HTA_LAYER_HIT"
_STD_COLLAPSE_EPSILON = 1e-6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_novel(name: str | None) -> bool:
    return bool(name) and name.startswith(NOVEL_PREFIX)


def _hypotheses_of(node) -> list[dict]:
    return list((node.payload or {}).get("hypotheses", []))


def _decision_nodes(graph: DecisionGraph, decision_type: str):
    return [
        n for n in graph.nodes.values()
        if n.kind == NodeKind.DECISION and n.decision_type == decision_type
    ]


def _scores(hyps: list[dict]) -> list[float]:
    return [float(h.get("score", 0.0)) for h in hyps]


def _std(scores: list[float]) -> float:
    if len(scores) < 2:
        return 0.0
    return statistics.pstdev(scores)


def _stuck_signal_bucket(stuck_reason: str | None) -> str | None:
    """Map a stuck_reason string to one of the 5 buckets the schema names."""
    if not stuck_reason:
        return None
    if stuck_reason.startswith("same_file_read_5x"):
        return "same_file_read_5x"
    if stuck_reason == "same_error_3x":
        return "same_error_3x"
    if stuck_reason == "budget_60pct_no_evidence":
        return "budget_60pct_no_evidence"
    if stuck_reason == "toolkit_repetition":
        return "toolkit_repetition"
    if stuck_reason in ("max_iterations", "no_action"):
        return "max_iterations"
    return None


# ---------------------------------------------------------------------------
# Per-section builders
# ---------------------------------------------------------------------------

def _rcl_section(graph: DecisionGraph) -> dict:
    rcl_nodes = _decision_nodes(graph, "root_cause_localization")
    if not rcl_nodes:
        return {
            "fired": False,
            "n_hypotheses": 0,
            "winner": None,
            "winner_predicted_path": None,
            "winner_class_is_novel": False,
            "winner_advantage": None,
            "std": None,
            "std_collapsed": False,
            "escalated": False,
            "any_tier2_call": False,
            "classes_seen": [],
        }
    # An RCL with a non-None winner is the "active" one — that's the
    # decision whose winner steered downstream execution.
    active = next(
        (n for n in reversed(rcl_nodes) if n.winner_hypothesis is not None),
        rcl_nodes[-1],
    )
    hyps = _hypotheses_of(active)
    # Escalation: any earlier RCL had escalated=true in its payload.
    escalated = any(
        bool((n.payload or {}).get("escalated", False)) for n in rcl_nodes
    )
    # Tier-2 was forced (per Phase E rule) on any novel-class hypothesis
    # across any RCL invocation in this episode.
    any_tier2 = any(
        _is_novel(h.get("name"))
        for n in rcl_nodes
        for h in _hypotheses_of(n)
    )
    classes_seen = sorted({
        h.get("name") for n in rcl_nodes for h in _hypotheses_of(n) if h.get("name")
    })
    winner_adv = None
    winner_pred_path = None
    for h in hyps:
        if h.get("name") == active.winner_hypothesis:
            winner_adv = float(h.get("advantage", 0.0))
            winner_pred_path = h.get("predicted_path") or None
            break
    scores = _scores(hyps)
    std = _std(scores)
    return {
        "fired": True,
        "n_hypotheses": len(hyps),
        "winner": active.winner_hypothesis,
        "winner_predicted_path": winner_pred_path,
        "winner_class_is_novel": _is_novel(active.winner_hypothesis),
        "winner_advantage": winner_adv,
        "std": std,
        "std_collapsed": std < _STD_COLLAPSE_EPSILON,
        "escalated": escalated,
        "any_tier2_call": any_tier2,
        "classes_seen": classes_seen,
    }


def _spec_interpretation_section(graph: DecisionGraph) -> dict:
    nodes = _decision_nodes(graph, "spec_interpretation")
    if not nodes:
        return {"fired": False, "trigger": None}
    # Currently the only trigger is RCL escalation. Future triggers
    # would extend this enum.
    return {"fired": True, "trigger": "rcl_escalation", "count": len(nodes)}


def _fix_locality_section(graph: DecisionGraph) -> dict:
    nodes = _decision_nodes(graph, "fix_locality_scope")
    if not nodes:
        return {
            "fired": False,
            "winner": None,
            "winner_advantage": None,
            "std": None,
            "std_collapsed": False,
            "sentinel_in_winner_payload": False,
            "sentinel_in_any_payload": False,
            "sentinel_count": 0,
            "gaming_detected": False,
            "gaming_detected_count": 0,
        }
    # If FL fires more than once (it usually doesn't), take the last.
    node = nodes[-1]
    hyps = _hypotheses_of(node)
    scores = _scores(hyps)
    std = _std(scores)
    sentinel_count = sum(
        1 for h in hyps if _LAYER_HIT_SENTINEL in (h.get("test_payload") or "")
    )
    sentinel_in_winner = False
    winner_adv = None
    for h in hyps:
        if h.get("name") == node.winner_hypothesis:
            winner_adv = float(h.get("advantage", 0.0))
            sentinel_in_winner = _LAYER_HIT_SENTINEL in (h.get("test_payload") or "")
            break
    # Issue H1 D2: count fix_locality decisions where the engine flagged
    # all-probes-fired-sentinel gaming. We track both the last-node value
    # (gaming_detected) and the per-issue count across all FL decisions
    # (gaming_detected_count) so the analyzer can report a run-wide rate.
    gaming_last = bool((node.payload or {}).get("gaming_detected", False))
    gaming_count = sum(
        1 for n in nodes if bool((n.payload or {}).get("gaming_detected", False))
    )
    return {
        "fired": True,
        "winner": node.winner_hypothesis,
        "winner_advantage": winner_adv,
        "std": std,
        "std_collapsed": std < _STD_COLLAPSE_EPSILON,
        "sentinel_in_winner_payload": sentinel_in_winner,
        "sentinel_in_any_payload": sentinel_count > 0,
        "sentinel_count": sentinel_count,
        "gaming_detected": gaming_last,
        "gaming_detected_count": gaming_count,
    }


def _investigation_continuation_section(graph: DecisionGraph) -> dict:
    ic_nodes = [
        n for n in graph.nodes.values()
        if n.kind == NodeKind.DECISION and n.decision_type == "investigation_continuation"
    ]
    # Order by node_id to find IC nodes in temporal order.
    ic_nodes.sort(key=lambda n: int(n.node_id[1:]) if n.node_id[1:].isdigit() else 0)
    breakdown = {
        "same_file_read_5x": 0,
        "same_error_3x": 0,
        "budget_60pct_no_evidence": 0,
        "toolkit_repetition": 0,
        "max_iterations": 0,
    }
    verdicts: list[str | None] = []
    # Map each IC node to the execution node that preceded it (which carries
    # the stuck_reason that triggered IC).
    exec_nodes = sorted(
        [n for n in graph.nodes.values() if n.kind == NodeKind.EXECUTION],
        key=lambda n: int(n.node_id[1:]) if n.node_id[1:].isdigit() else 0,
    )
    for ic in ic_nodes:
        verdicts.append(ic.winner_hypothesis)
        ic_idx = int(ic.node_id[1:]) if ic.node_id[1:].isdigit() else -1
        # Last execution node before this IC.
        preceding = None
        for ex in exec_nodes:
            ex_idx = int(ex.node_id[1:]) if ex.node_id[1:].isdigit() else -1
            if ex_idx < ic_idx:
                preceding = ex
            else:
                break
        if preceding is not None:
            reason = (preceding.payload or {}).get("stuck_reason")
            bucket = _stuck_signal_bucket(reason)
            if bucket:
                breakdown[bucket] += 1
    return {
        "fired_count": len(ic_nodes),
        "signal_breakdown": breakdown,
        "verdicts": verdicts,
    }


def _execution_section(graph: DecisionGraph) -> dict:
    exec_nodes = [
        n for n in graph.nodes.values()
        if n.kind == NodeKind.EXECUTION and (n.payload or {}).get("iterations") is not None
    ]
    total = sum(int((n.payload or {}).get("iterations", 0)) for n in exec_nodes)
    stuck = sum(1 for n in exec_nodes if (n.payload or {}).get("stuck"))
    return {
        "count": len(exec_nodes),
        "total_iterations": total,
        "stuck_count": stuck,
    }


def _budget_section(initial: int | None, final: int | None) -> dict:
    if initial is None:
        return {"initial": None, "used": None, "fraction_used": None}
    used = (initial - final) if final is not None else None
    frac = (used / initial) if (used is not None and initial > 0) else None
    return {"initial": initial, "used": used, "fraction_used": frac}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_summary(
    issue_id: str,
    branch: str,
    graph: DecisionGraph,
    initial_budget: int | None,
    final_budget: int | None,
    tier2_calls_used: int,
    memory_distillations_emitted: int = 0,
    memory_distillation_cap: int = 0,
) -> dict[str, Any]:
    """Walk a completed graph and produce the per-episode summary dict."""
    rcl = _rcl_section(graph)
    spec = _spec_interpretation_section(graph)
    fix_locality = _fix_locality_section(graph)
    ic = _investigation_continuation_section(graph)
    execs = _execution_section(graph)
    budget = _budget_section(initial_budget, final_budget)
    decision_count = sum(
        1 for n in graph.nodes.values() if n.kind == NodeKind.DECISION
    )
    backward_edges = sum(1 for e in graph.edges if e.kind == "backward")
    # Issue H1 D3: count how many times spec_interpretation escalation
    # actually fired this issue. rcl.escalated is a boolean derived from
    # any-decision-with-escalated-payload; this field is the integer
    # count so the analyzer can show "up to N per issue" behaviour.
    escalation_count = sum(
        1 for n in graph.nodes.values()
        if (n.payload or {}).get("escalated")
    )
    return {
        "issue_id": issue_id,
        "branch": branch,
        "rcl": rcl,
        "spec_interpretation": spec,
        "fix_locality": fix_locality,
        "investigation_continuation": ic,
        "execution_nodes": execs,
        "budget": budget,
        "decision_count": decision_count,
        "graph_node_count": len(graph.nodes),
        "backward_edge_count": backward_edges,
        "tier2_calls_used": tier2_calls_used,
        "escalation_count": escalation_count,
        # Issue H3: per-episode semantic-memory distillation accounting.
        "memory": {
            "distillations_emitted": memory_distillations_emitted,
            "distillation_cap": memory_distillation_cap,
        },
    }


def write_summary(summary: dict, run_dir: str) -> str:
    """Atomically write the summary to ``<run_dir>/analysis/<issue_id>.json``.

    Returns the final path. Caller should wrap in try/except — a write
    failure must not propagate to the engine.
    """
    out_dir = Path(run_dir) / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{summary['issue_id']}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2))
    os.replace(tmp, target)
    return str(target)
