"""Post-run HTA evaluation aggregator (issue #46 Fix 3).

Reads the per-episode analysis summaries and outcomes from an HTA run
plus a DAG baseline directory, joins them, and emits the 6-table
markdown report the verdict author reviews.

Usage:
    python scripts/analyze_hta_run.py \
        --run-dir .midas/train/hta_eval_30 \
        --dag-baseline-dir artifacts/checkpoint \
        --output .midas/train/hta_eval_30/report.md

The DAG baseline dir is expected to contain a ``data/`` subdirectory with
``ep<idx>_<issue_id>.json`` files for passing episodes and
``fail<idx>_<issue_id>.json`` files for failing ones (the existing
Midas checkpoint format).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Make the script runnable directly without installing midas_agent.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from midas_agent.workspace.hta.analysis.patch_classifier import (  # noqa: E402
    action_family, classify,
)


TOKEN_PER_ITERATION_ESTIMATE = 5_000


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_outcomes_jsonl(run_dir: Path) -> dict[str, dict]:
    path = run_dir / "outcomes.jsonl"
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        iid = rec.get("issue_id")
        if iid:
            out[iid] = rec
    return out


def _read_patch_for_issue(run_dir: Path, issue_id: str, summaries: dict) -> str:
    """The current writer uses random hex filenames. We match patches to
    issues by the order they appear in outcomes.jsonl (lines are appended
    in episode-completion order) vs the patches/ws-0/ dir sorted by mtime.
    This is best-effort — if the join fails for an issue, the analyzer
    reports patch="" and falls back to "mixed" classification.
    """
    # Build the mtime-sorted patch list once and cache on the dict.
    cache_key = "__patch_list__"
    if cache_key not in summaries:
        pdir = run_dir / "log" / "patches" / "ws-0"
        if pdir.exists():
            patches = sorted(pdir.glob("*.patch"), key=lambda p: p.stat().st_mtime)
        else:
            patches = []
        summaries[cache_key] = patches
    patches = summaries[cache_key]
    # outcomes.jsonl is append-ordered; we expect issues processed in the
    # same order they appear in outcomes.jsonl. Build an index.
    order_key = "__issue_order__"
    if order_key not in summaries:
        outcomes_path = run_dir / "outcomes.jsonl"
        order: list[str] = []
        if outcomes_path.exists():
            for line in outcomes_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                iid = rec.get("issue_id")
                if iid and iid not in order:
                    order.append(iid)
        summaries[order_key] = order
    order = summaries[order_key]
    try:
        idx = order.index(issue_id)
    except ValueError:
        return ""
    if idx >= len(patches):
        return ""
    return patches[idx].read_text()


def load_run(run_dir: Path) -> dict[str, dict]:
    """Return ``{issue_id: {summary, outcome, patch}}`` for all episodes
    that produced an analysis JSON."""
    analysis_dir = run_dir / "analysis"
    out: dict[str, dict] = {}
    if not analysis_dir.exists():
        return out
    outcomes = _load_outcomes_jsonl(run_dir)
    for fp in sorted(analysis_dir.glob("*.json")):
        try:
            summary = json.loads(fp.read_text())
        except json.JSONDecodeError:
            continue
        iid = summary.get("issue_id") or fp.stem
        out[iid] = {
            "summary": summary,
            "outcome": outcomes.get(iid),
            "patch": "",  # filled below
        }
    # Resolve patches with a small cache.
    cache: dict = {}
    for iid, rec in out.items():
        rec["patch"] = _read_patch_for_issue(run_dir, iid, cache)
    return out


def load_dag_baseline(baseline_dir: Path) -> dict[str, dict]:
    """Read ``<baseline>/data/ep*_<issue>.json`` (pass) and
    ``fail*_<issue>.json`` (fail). Returns ``{issue_id: {passed: bool,
    score: float|None}}``.
    """
    data_dir = baseline_dir / "data"
    if not data_dir.exists():
        data_dir = baseline_dir  # caller may have pointed straight at data/
    if not data_dir.exists():
        return {}
    out: dict[str, dict] = {}
    rx = re.compile(r"^(ep|fail)\d+_(.+)\.json$")
    for fp in sorted(data_dir.glob("*.json")):
        m = rx.match(fp.name)
        if not m:
            continue
        kind, iid = m.group(1), m.group(2)
        try:
            data = json.loads(fp.read_text())
        except json.JSONDecodeError:
            data = {}
        # Latest result wins on duplicates.
        out[iid] = {
            "passed": (kind == "ep"),
            "score": data.get("score"),
        }
    return out


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _passed(rec: dict) -> bool | None:
    o = rec.get("outcome") or {}
    if "passed" in o:
        return bool(o["passed"])
    if "s_exec" in o:
        return (o.get("s_exec") or 0.0) >= 1.0
    return None


def _classify_patch_for(rec: dict) -> str:
    return classify(rec.get("patch") or "")


def _patch_files(patch_text: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+)", patch_text or "", re.MULTILINE)


def _check_mark(v: bool | None) -> str:
    if v is True:
        return "✅"
    if v is False:
        return "❌"
    return "—"


def table_1_per_episode(run: dict, dag: dict) -> str:
    lines = [
        "## Table 1 — Per-episode outcome and mechanism summary",
        "",
        "| Issue | HTA | DAG | RCL discriminate | Escalated | fix_loc discriminate | Sentinel used | IC fires | Patch type |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for iid in sorted(run):
        rec = run[iid]
        s = rec["summary"]
        hta = _passed(rec)
        d = dag.get(iid)
        dag_pass = d["passed"] if d else None
        rcl = s.get("rcl", {})
        fl = s.get("fix_locality", {})
        ic = s.get("investigation_continuation", {})
        cat = _classify_patch_for(rec)
        lines.append(
            f"| {iid} | {_check_mark(hta)} | {_check_mark(dag_pass)} | "
            f"{'yes' if rcl.get('fired') and not rcl.get('std_collapsed') else 'no'} | "
            f"{'yes' if rcl.get('escalated') else 'no'} | "
            f"{'yes' if fl.get('fired') and not fl.get('std_collapsed') else 'no'} | "
            f"{'yes' if fl.get('sentinel_in_any_payload') else 'no'} | "
            f"{ic.get('fired_count', 0)} | "
            f"{cat} |"
        )
    return "\n".join(lines)


def table_2_outcome(run: dict, dag: dict) -> str:
    N = len(run)
    hta_wins = sum(1 for r in run.values() if _passed(r) is True)
    paired = [(iid, _passed(r), dag.get(iid, {}).get("passed"))
              for iid, r in run.items()]
    paired_known = [(i, h, d) for (i, h, d) in paired if d is not None]
    M = len(paired_known)
    dag_wins = sum(1 for (_, _, d) in paired_known if d is True)
    both_win = sum(1 for (_, h, d) in paired_known if h is True and d is True)
    both_fail = sum(1 for (_, h, d) in paired_known if h is False and d is False)
    hta_flips = sum(1 for (_, h, d) in paired_known if h is True and d is False)
    dag_flips = sum(1 for (_, h, d) in paired_known if d is True and h is False)
    pct = lambda x, n: f"({x*100/n:.1f}%)" if n else "(—)"
    return (
        "## Table 2 — Outcome comparison\n\n"
        f"```\n"
        f"HTA wins:    {hta_wins} / {N}  {pct(hta_wins, N)}\n"
        f"DAG wins:    {dag_wins} / {M}  {pct(dag_wins, M)}   (DAG baseline available on {M} issues)\n"
        f"Both win:    {both_win} / {M}\n"
        f"Both fail:   {both_fail} / {M}\n"
        f"HTA flips:   {hta_flips} / {M}   (HTA ✅ where DAG ❌)\n"
        f"DAG flips:   {dag_flips} / {M}   (DAG ✅ where HTA ❌)\n"
        f"Net flip:    {hta_flips - dag_flips}  in HTA's favour\n"
        f"```"
    )


def table_3_activation(run: dict) -> str:
    N = len(run)
    pct = lambda x, n: f"({x*100/n:.0f}%)" if n else "(—)"

    rcl_discrim = sum(
        1 for r in run.values()
        if r["summary"].get("rcl", {}).get("fired")
        and not r["summary"]["rcl"].get("std_collapsed")
    )
    rcl_escalated = sum(
        1 for r in run.values()
        if r["summary"].get("rcl", {}).get("escalated")
    )
    spec_fired = sum(
        1 for r in run.values()
        if r["summary"].get("spec_interpretation", {}).get("fired")
    )
    fl_discrim = sum(
        1 for r in run.values()
        if r["summary"].get("fix_locality", {}).get("fired")
        and not r["summary"]["fix_locality"].get("std_collapsed")
    )
    sentinel_winner = sum(
        1 for r in run.values()
        if r["summary"].get("fix_locality", {}).get("sentinel_in_winner_payload")
    )
    sentinel_any = sum(
        1 for r in run.values()
        if r["summary"].get("fix_locality", {}).get("sentinel_in_any_payload")
    )
    fl_gaming = sum(
        1 for r in run.values()
        if r["summary"].get("fix_locality", {}).get("gaming_detected_count", 0) > 0
    )
    ic_any = sum(
        1 for r in run.values()
        if r["summary"].get("investigation_continuation", {}).get("fired_count", 0) > 0
    )
    ic_via_bypass = sum(
        r["summary"]["investigation_continuation"]["signal_breakdown"].get("same_file_read_5x", 0)
        for r in run.values()
    )
    ic_via_meta = sum(
        sum(v for k, v in r["summary"]["investigation_continuation"]["signal_breakdown"].items()
            if k != "same_file_read_5x")
        for r in run.values()
    )
    novel_any = sum(
        1 for r in run.values()
        if r["summary"].get("rcl", {}).get("any_tier2_call")
    )
    tier2_total = sum(
        r["summary"].get("tier2_calls_used", 0) for r in run.values()
    )
    tier2_any = sum(
        1 for r in run.values() if r["summary"].get("tier2_calls_used", 0) > 0
    )
    return (
        "## Table 3 — Mechanism activation rates\n\n"
        f"```\n"
        f"RCL discriminates (std > epsilon):                  {rcl_discrim} / {N}  {pct(rcl_discrim, N)}\n"
        f"RCL collapses & escalates:                          {rcl_escalated} / {N}  {pct(rcl_escalated, N)}\n"
        f"spec_interpretation fires (via escalation):         {spec_fired} / {N}\n"
        f"fix_locality discriminates:                         {fl_discrim} / {N}  {pct(fl_discrim, N)}\n"
        f"  - sentinel in winner's test_payload:              {sentinel_winner} / {N}\n"
        f"  - sentinel in any test_payload:                   {sentinel_any} / {N}\n"
        f"  - gaming detected (all probes scored 1.0):        {fl_gaming} / {N}\n"
        f"IC fires at least once:                             {ic_any} / {N}\n"
        f"  - via same_file_read_5x (bypass path):            {ic_via_bypass}\n"
        f"  - via meta-judge:                                 {ic_via_meta}\n"
        f"Novel class hypothesis emitted (any decision):      {novel_any} / {N}\n"
        f"Tier-2 LLM call used (any decision):                {tier2_any} / {N}   (total calls: {tier2_total})\n"
        f"```"
    )


def table_4_quadrant(run: dict, dag: dict) -> str:
    rows = ["remove", "add", "modify"]
    cols = [
        ("RCL discriminates", lambda r:
            r["summary"]["rcl"].get("fired") and not r["summary"]["rcl"].get("std_collapsed")
            and not r["summary"]["rcl"].get("escalated")),
        ("RCL collapses & escalates", lambda r:
            r["summary"]["rcl"].get("escalated")),
    ]
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for iid, rec in run.items():
        d = dag.get(iid)
        if d is None:
            continue  # need baseline for HTA vs DAG comparison
        cat = _classify_patch_for(rec)
        fam = action_family(cat)
        if fam not in rows:
            continue
        for col_name, fn in cols:
            try:
                in_col = bool(fn(rec))
            except Exception:
                in_col = False
            if in_col:
                cells[(fam, col_name)].append((rec, d))
    lines = [
        "## Table 4 — The 4-quadrant question",
        "",
        "Each cell shows `HTA wins/total · DAG wins/total` on episodes that "
        "fall in that combination of (RCL outcome × patch action family).",
        "",
        "| | " + " | ".join(c[0] for c in cols) + " |",
        "|---|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for fam in rows:
        row = [f"**Patch action: {fam}-type**"]
        for c in cols:
            bucket = cells.get((fam, c[0]), [])
            if not bucket:
                row.append("—")
                continue
            total = len(bucket)
            hta_wins = sum(1 for (r, _) in bucket if _passed(r) is True)
            dag_wins = sum(1 for (_, d) in bucket if d.get("passed") is True)
            row.append(f"HTA {hta_wins}/{total} · DAG {dag_wins}/{total}")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def table_5_failure_modes(run: dict, dag: dict) -> str:
    """Categorise HTA losses into the 6 buckets the spec lists."""
    categories: dict[str, list[str]] = defaultdict(list)

    for iid, rec in run.items():
        if _passed(rec) is not False:
            continue  # only losses
        s = rec["summary"]
        rcl = s.get("rcl", {})
        fl = s.get("fix_locality", {})
        ic = s.get("investigation_continuation", {})
        budget = s.get("budget", {})
        execs = s.get("execution_nodes", {})
        patch_text = rec.get("patch") or ""
        patch_files = _patch_files(patch_text)
        cat = _classify_patch_for(rec)
        winner_pred = rcl.get("winner_predicted_path") or ""

        # Categorise — first match wins.
        # Budget exhaustion: ran out of budget with little patch material.
        if (budget.get("fraction_used") or 0.0) >= 0.95 and len(patch_text.strip()) < 200:
            categories["Budget exhaustion (ran out before solving)"].append(iid)
            continue
        # Mid-execution flail: long stuck node without effective IC.
        max_iters = execs.get("count", 0) and max(
            (n.get("iterations") or 0)
            for n in [{"iterations": (e or {}).get("iterations")}
                      for e in [None]]
        )
        # The above max-trick is awkward; recompute by reading raw payloads
        # would require parsing graphs. Use stuck_count + IC verdicts.
        if (execs.get("stuck_count", 0) >= 1
                and (ic.get("fired_count", 0) == 0
                     or all(v == "persist_same_path" for v in ic.get("verdicts", [])))):
            categories["Mid-execution flail (long stuck, IC didn't help)"].append(iid)
            continue
        # Wrong root cause class: winner's predicted_path file not in patch files.
        if winner_pred and not any(winner_pred in pf or pf in winner_pred for pf in patch_files):
            categories["Wrong root cause class (RCL picked badly)"].append(iid)
            continue
        # Wrong action type: RCL discriminated + add-type patch + DAG won via remove-type.
        dag_rec = dag.get(iid)
        if (rcl.get("winner_advantage") or 0.0) > 0.8 and action_family(cat) == "add":
            if dag_rec and dag_rec.get("passed"):
                categories["Wrong action type (RCL right, patch did wrong thing)"].append(iid)
                continue
        # Gold-test format wall: decision_count >= 2, winner_advantage > 0.5,
        # patch is classifiable (not "mixed"), and patch is non-trivial.
        if (s.get("decision_count", 0) >= 2
                and (rcl.get("winner_advantage") or 0.0) > 0.5
                and cat != "mixed"
                and len(patch_text.strip()) > 200):
            categories["Gold-test format wall (patch good but bit-exact)"].append(iid)
            continue
        categories["Other"].append(iid)

    lines = [
        "## Table 5 — Failure-mode breakdown (HTA losses only)",
        "",
        "| Failure category | Count | Example issue |",
        "|---|---|---|",
    ]
    bucket_order = [
        "Gold-test format wall (patch good but bit-exact)",
        "Wrong root cause class (RCL picked badly)",
        "Wrong action type (RCL right, patch did wrong thing)",
        "Mid-execution flail (long stuck, IC didn't help)",
        "Budget exhaustion (ran out before solving)",
        "Other",
    ]
    for name in bucket_order:
        bucket = categories.get(name, [])
        ex = bucket[0] if bucket else "—"
        lines.append(f"| {name} | {len(bucket)} | {ex} |")
    return "\n".join(lines)


def table_6_cost(run: dict, dag: dict) -> str:
    hta_iters = sum(
        r["summary"].get("execution_nodes", {}).get("total_iterations", 0)
        for r in run.values()
    )
    # DAG iteration estimate from baseline data, if available. The baseline
    # JSON has a `trace` field (string); count [iter N] occurrences.
    dag_iters = 0
    for iid in run:
        d = dag.get(iid)
        if d is None:
            continue
        # We don't reload the trace text here; approximate by 50 iters/issue
        # (the published 20-issue averages were 31–94, median ~50).
        dag_iters += 50
    hta_tokens = hta_iters * TOKEN_PER_ITERATION_ESTIMATE
    dag_tokens = dag_iters * TOKEN_PER_ITERATION_ESTIMATE
    ratio = (hta_tokens / dag_tokens) if dag_tokens else None
    avg_dp = (
        sum(r["summary"].get("decision_count", 0) for r in run.values())
        / max(1, len(run))
    )
    avg_ic = (
        sum(r["summary"].get("investigation_continuation", {}).get("fired_count", 0)
            for r in run.values())
        / max(1, len(run))
    )
    avg_tier2 = (
        sum(r["summary"].get("tier2_calls_used", 0) for r in run.values())
        / max(1, len(run))
    )
    ratio_str = f"{ratio:.2f}" if ratio else "n/a"
    return (
        "## Table 6 — Cost & overhead\n\n"
        "Token totals are estimated as iteration_count × "
        f"{TOKEN_PER_ITERATION_ESTIMATE:,} (placeholder, "
        "stated explicitly per spec). DAG iterations are approximated at 50/issue "
        "(the README's per-issue range was 31–94, median ~50). Treat ratios as orders of magnitude.\n\n"
        f"```\n"
        f"Total tokens across {len(run)} issues (HTA, est.):  {hta_tokens:,}\n"
        f"Total tokens across DAG baseline (est.):            {dag_tokens:,}\n"
        f"HTA / DAG ratio:                                    {ratio_str}\n"
        f"Average decision points per issue:                  {avg_dp:.1f}\n"
        f"Average IC calls per issue:                         {avg_ic:.2f}\n"
        f"Average Tier-2 calls per issue:                     {avg_tier2:.2f}  (capped at 3)\n"
        f"```"
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def generate_report(run: dict, dag: dict) -> str:
    parts = [
        "# HTA evaluation report",
        "",
        f"_Generated from `{len(run)}` complete HTA episodes "
        f"and `{len(dag)}` DAG baseline issues._",
        "",
        table_1_per_episode(run, dag),
        "",
        table_2_outcome(run, dag),
        "",
        table_3_activation(run),
        "",
        table_4_quadrant(run, dag),
        "",
        table_5_failure_modes(run, dag),
        "",
        table_6_cost(run, dag),
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--dag-baseline-dir", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    run = load_run(args.run_dir)
    dag = load_dag_baseline(args.dag_baseline_dir)
    if not run:
        print(f"WARNING: no analysis files under {args.run_dir}/analysis/",
              file=sys.stderr)
    report = generate_report(run, dag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report)
    print(f"Wrote report ({len(report)} chars) -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
