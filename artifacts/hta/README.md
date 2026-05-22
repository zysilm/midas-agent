# HTA evaluation artifacts

Three full 30-issue HTA evaluation runs on the same SWE-bench-Verified
slice (22 astropy + 8 django, 2026-05-20 → 2026-05-22). Each subfolder
holds the report-level + memory-level outputs of one run; raw docker
logs, per-iteration action dumps, and per-workspace trajs are not
retained here (they live under `.midas/train/` if still present).

| Subfolder | Run name | Branch | HTA pass rate | Notes |
|---|---|---|---|---|
| `baseline/` | `hta_eval_30` (pre-H1) | `feature/issue-46-eval-and-verdict` | **14/30 (46.7%)** | First instrumented end-to-end run; produced `VERDICT.md` and the original 847 KB `all_episode_traces.md`. |
| `h1/` | `hta_eval_30_h1` | `fix/hta-verifier-fixes` | **15/30 (50.0%)** | Three verifier-side fixes: ContinuationVerifier reads `stuck_reason` (D1), FixLocality anti-gaming detection (D2), spec_interpretation re-entry counter (D3). |
| `h3_cold/` | `hta_eval_30_h3` | `feature/hta-semantic-memory` | **15/30 (50.0%)** | Numerical advantage memory replaced by `SemanticExperienceMemory` (one distillation LLM call per decision point; narrative `bias_summary` injection). 85 memory entries at end of run. See `H3_COLD_REPORT.md` for the full mechanism-health writeup. |

The user's prompt described H1 as "16/30"; the actual analyzer count
(in `h1/report.md` Table 2) is 15/30 — using the actual count above.

## Per-subfolder contents

Each subfolder holds the same shape:

- `report.md` — the 6-table (H3 adds Table 7) aggregator output from
  `scripts/analyze_hta_run.py`. Cross-issue numbers, mechanism activation
  rates, 4-quadrant analysis, failure-mode breakdown, cost estimate.
- `episode_traces_compact.md` — per-episode breakdown (decision graph +
  compacted ReAct trace + patch diff). The H1/H3 standard compact format.
- `outcomes.jsonl` — one line per (issue_id, workspace_id, s_exec, s_w,
  passed) record. The source-of-truth pass/fail data.
- `analysis/` — 30 per-episode JSON summaries produced by
  `midas_agent.workspace.hta.analysis.episode_summary.build_summary`.
  Contains RCL / fix_locality / spec_interp / IC / execution_nodes
  sections plus (H1+) gaming-detected and (H3+) memory accounting.
- `advantage_memory.json` — the final cross-issue memory store at
  end-of-run. **Baseline** and **H1** carry the numerical
  `TypedAdvantageMemory` schema (per-cell mean/m2/count); **H3-cold**
  carries the semantic `SemanticExperienceMemory` schema-v2 (entries
  list of narrative `SemanticMemoryEntry` records — 85 entries).

`baseline/` additionally holds:

- `VERDICT.md` — the multi-section narrative verdict written at the end
  of the baseline run (recommendation C, "HTA architecture is not
  paying off; pivot to CFL"). Subsequent runs do not produce
  VERDICT.md — the user paused that workflow after the baseline.
- `all_episode_traces.md` — the original full-trace file (847 KB,
  per-iteration ReAct dumps). Superseded by the `episode_traces_compact.md`
  format used in H1/H3, but kept here for completeness on the baseline.

## A note on "DAG baseline" columns

Each `report.md` has columns showing how DAG (the legacy linear-ReAct
runtime) performed on the same issues. These come from previously-
checkpointed DAG runs under `artifacts/checkpoint/data/` —
**they are paired per-issue comparison data, not a separate DAG trace
run produced in this evaluation**. No `artifacts/dag/` folder exists
because there is no standalone DAG trace dataset to archive; if you
want the raw DAG-side traces, look under `artifacts/checkpoint/`.
