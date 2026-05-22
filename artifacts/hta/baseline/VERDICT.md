# HTA 30-issue evaluation — verdict

Run directory: `.midas/train/hta_eval_30/`
Branch: `feature/issue-46-eval-and-verdict`
Issues attempted: 30
Issues with complete data: 30 (all)
Issues with paired DAG baseline: 29 (astropy-7336 has no baseline)
Run date range: 2026-05-20 18:12 to 2026-05-20 23:22  (~5h10m, 100% of episodes succeeded)

## 1. Headline numbers

- **HTA wins: 14 / 30 (46.7%)**
- **DAG baseline wins: 15 / 29 paired (51.7%)**
- **Net flip in HTA's favour: −2** (0 HTA wins where DAG lost, 2 DAG wins where HTA lost: `django-10880`, `django-10914`)
- HTA-only pass (no DAG paired data): `astropy-7336` ✅

After all the #44/#45 instrumentation, mechanism, and prompt-sync fixes, HTA's solve rate on the same model + same issues is **0 issues better than DAG, 2 issues worse**.

## 2. Did the #44 / #45 fixes do what they were supposed to do?

Reference: Table 3 of `report.md`.

| Mechanism | Target | Actual | Status |
|---|---|---|---|
| RCL discrimination | "majority" | **29 / 30 (97%)** | ✅ working |
| Sentinel adoption at `fix_locality` (Fix 1, #45) | ≥75% | **28 / 30 (93%)** | ✅ working |
| IC activation when stuck signals fire (Fix 2, #45) | ≥80% via bypass | **24 IC invocations, 100% via `same_file_read_5x` bypass; 0 via meta-judge** | ✅ working (bypass) ⚠️ (meta-judge path dormant) |
| Tier-2 forcing for novel classes (#44 C2) | triggered when novel emitted | **52 Tier-2 calls in 27 / 30 episodes** | ✅ working |
| Spec_interpretation reachability (#44 B7) | reachable | **6 / 30 episodes escalated** | ✅ working |

**Verdict: ✅ working across the board.** The mechanism fires as designed. The disconnect is between mechanism activation and solve rate — high activation does not translate to wins.

## 3. The strategic question — does `spec_interpretation` need to be reachable independent of RCL collapse?

Reference: Table 4 of `report.md`.

The 4-quadrant cells, with current data:

| | RCL discriminates | RCL collapses & escalates |
|---|---|---|
| Patch action: remove-type | HTA 1/1 · DAG 1/1 | — *(0 cases)* |
| Patch action: add-type | HTA 4/8 · DAG 4/8 | HTA 0/2 · DAG 0/2 |
| Patch action: modify-type | HTA 6/14 · DAG 8/14 | HTA 2/4 · DAG 2/4 |

The specific Q3 comparison (`RCL-collapses × remove-type` vs `RCL-discriminates × remove-type`) is **not answerable from this data**: the `RCL-collapses × remove-type` cell contains 0 episodes. Across all `RCL-collapses` cells the total is only 6 issues, and within those, 0 fall into `remove-type`. The cell needed to test the hypothesis is empty.

What the data *does* show: HTA underperforms DAG specifically in the `RCL-discriminates × modify-type` cell (HTA 6/14 vs DAG 8/14 — both `django-10880` and `django-10914` regressions live here). Both were issues where the agent had high-confidence RCL guidance, picked a modify-type patch, and over-narrowed it.

**Verdict on Q3: unclear due to thin sample.** The cell that would resolve "does escalation produce a useful framing remove-type can't reach otherwise" is empty in this run. The `ep3-13236-v1 → v2` reversal that motivated this question reproduced as `ep3 v1 ✅ → v2 ❌ → eval ❌` — confirming v1 was *isolated*, not a systematic mechanism advantage. But the broader question of whether escalation is *valuable* is not testable from 30 issues.

## 4. What is the dominant failure mode on HTA losses?

Reference: Table 5 of `report.md`. Top categories on HTA's 16 losses:

| Category | Count | HTA-attributable? | Example |
|---|---|---|---|
| **Mid-execution flail (long stuck, IC didn't help)** | **13** | yes — mechanism design | `astropy-13033` |
| Wrong root cause class (RCL picked badly) | 2 | yes — mechanism design | `astropy-13398` |
| Gold-test format wall (patch good but bit-exact) | 1 | no — external (SWE-bench) | `django-10097` |

**The dominant failure mode is mid-execution flail in execution nodes** (13/16 losses). `investigation_continuation` *fires* on these — but its verdict is **always `persist_same_path`** (per the per-episode analysis: 0 pivots, 0 abandons, 0 pivot_targets across the entire run). The rescue mechanism's behaviour has collapsed to "keep trying", which on flail-pattern issues is exactly the wrong advice.

This is HTA-attributable: the structure that should rescue stuck agents has no effective rescue verdict. IC is firing but not deciding.

## 5. Cost-benefit assessment

Reference: Table 6 of `report.md` — **with a correction**.

The aggregator's Table 6 estimates DAG iterations at 50/issue (placeholder per spec). The README's head-to-head table has **actual per-issue DAG tokens** for the 20 astropy issues. Using real DAG data:

| Source | HTA (estimate, 5K/iter) | DAG (README actual or estimate) |
|---|---|---|
| 20 astropy paired issues | **8.0M tokens** | **17.8M tokens** (README, same model) |
| Per-issue average | 400K | 890K |

**HTA / DAG token ratio: 0.45× on the astropy subset with real DAG data.**

Even with the aggregator's blanket 5K/iter estimate (which likely undercounts HTA's large-context iters), HTA averages 80 iterations/issue against DAG's published 47 — wait, that's iters. Tokens-wise: DAG's README average is 890K/issue; HTA's iters × 5K = 400K/issue. HTA uses **about half the tokens** despite having more iterations, because DAG's iterations have on average ~19K tokens each (large per-iter context) vs HTA's mostly smaller execution-node contexts.

Per-flip cost:
- HTA passes: 14 issues, ~12M tokens total ⇒ **0.86M tokens/pass**
- DAG passes: 15 issues paired, ~25.8M tokens (extrapolating from 890K avg) ⇒ **1.72M tokens/pass**

**Verdict: HTA is more cost-efficient per pass than DAG.** ~2× cheaper per issue solved. But the net outcome is fewer issues solved (−2). The trade is "spend half the tokens, solve 2 fewer issues" — a real engineering trade-off whose worthiness depends on whether token cost or pass rate is the bottleneck.

## 6. Recommendation

**C — HTA architecture is not paying off; pivot to CFL.**

Citation of supporting cells:
- Table 2 line "Net flip: −2 in HTA's favour" — HTA has *zero* wins where DAG lost across 29 paired issues.
- Table 5 line "Mid-execution flail … 13" — 81% of HTA losses are caused by a mechanism that *fires* but doesn't *decide* (IC always returns `persist_same_path`).
- Table 4 line "Patch action: modify-type / RCL discriminates: HTA 6/14 · DAG 8/14" — HTA's worst cell, both regressions live here, no compensating wins.

The cost-efficiency finding (HTA ≈ 0.5× DAG tokens) is real and meaningful — but it's a secondary property. The primary value proposition of HTA was structural cross-issue learning and better steering, neither of which materialises. The cost win is a side effect of HTA's backbone being shorter, not of its decision layer being smart.

CFL (#41 design) targets exactly the failure mode this eval identifies as dominant — uncommitted prediction inside execution nodes — at much lower implementation complexity. Issue #45's pre-existing analysis projected CFL would catch ~80% of the failure modes observed. The eval data is consistent with that projection: 13/16 HTA losses are in-execution-node failures that an in-loop falsification hook would target directly, where HTA's between-node decision points cannot.

**Confidence: medium.** 30 issues is a moderate sample, single-language astropy was 22 of 30, and the absence of HTA flips could in principle reverse on a different 30 (we'd need to see HTA flip at least one issue to know the architecture *can* win in this configuration). But the dominant failure mode finding is clear and structural — IC firing 24 times with 0 useful verdicts is a design defect, not a sample-size artefact.

## 7. Open questions left for next iteration

1. **`investigation_continuation`'s verdict space has collapsed to `persist_same_path`.** Across 30 episodes the IC verdict was `persist_same_path` 100% of the time (0 `pivot_target`, 0 `pivot_evidence_type`, 0 `abandon`). The mechanism *fires* but doesn't *decide*. Before CFL pivot, worth one experiment: hand-tune `ContinuationVerifier` so non-persist verdicts can win. If still 100% persist after tuning, the design itself is flawed (the verifier rewards "kept trying" too easily). Either result is informative.

2. **2/30 episodes had truncated graphs** (only 1 DP, 1 execution node — eps 15 and 24). Cause not investigated in this eval. Likely an interaction between stuck-detection + meta-judge declining + worklist behaviour. If HTA continues, file as a bug; if pivoting to CFL, no action needed.

3. **Tier-2 LLM judge endorses novel classes whose slugs are creative but often misleading.** 24/30 episodes had novel-class hypotheses; some are genuinely meaningful (`recursive_bypass`, `slice_coordinate_mapping`), others are bizarre (`automatic_convenience_pedestal` on 13236, which produced an *empty patch*). The current Tier-2 prompt rewards lexical plausibility, not problem fit. If HTA continues, sharpen the judge prompt; the ep3-empty-patch failure mode shouldn't be possible.

4. **Token-cost finding deserves its own follow-up.** HTA at ~0.5× DAG tokens is a real win, even with the −2 flip. A "cheap HTA" hybrid — keep the backbone's execution-node guidance, drop the novel-class Tier-2 forcing and IC — might preserve the cost win without the failure-mode regression. Not in scope here, but worth a one-page design note before pivoting.
