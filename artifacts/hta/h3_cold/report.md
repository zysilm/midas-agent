# HTA evaluation report

_Generated from `30` complete HTA episodes and `96` DAG baseline issues._

## Table 1 — Per-episode outcome and mechanism summary

| Issue | HTA | DAG | RCL discriminate | Escalated | fix_loc discriminate | Sentinel used | IC fires | Patch type |
|---|---|---|---|---|---|---|---|---|
| astropy__astropy-12907 | ✅ | ✅ | yes | yes | yes | yes | 0 | modify_logic |
| astropy__astropy-13033 | ❌ | ❌ | yes | no | no | no | 2 | add_guard |
| astropy__astropy-13236 | ❌ | ❌ | yes | no | yes | yes | 1 | add_warning |
| astropy__astropy-13398 | ❌ | ❌ | yes | no | yes | yes | 0 | add_branch |
| astropy__astropy-13453 | ✅ | ✅ | yes | no | no | no | 1 | mixed |
| astropy__astropy-13579 | ✅ | ✅ | yes | no | no | yes | 2 | add_branch |
| astropy__astropy-13977 | ❌ | ❌ | yes | no | no | yes | 2 | mixed |
| astropy__astropy-14096 | ❌ | ✅ | yes | no | yes | yes | 1 | add_guard |
| astropy__astropy-14182 | ❌ | ❌ | yes | no | no | yes | 1 | add_branch |
| astropy__astropy-14309 | ✅ | ✅ | yes | no | yes | yes | 0 | add_guard |
| astropy__astropy-14365 | ✅ | ❌ | yes | no | yes | yes | 1 | modify_logic |
| astropy__astropy-14369 | ❌ | ✅ | yes | no | no | no | 1 | add_branch |
| astropy__astropy-14508 | ✅ | ✅ | yes | no | yes | yes | 0 | add_guard |
| astropy__astropy-14539 | ✅ | ✅ | yes | no | no | yes | 1 | modify_logic |
| astropy__astropy-14598 | ❌ | ❌ | yes | no | no | no | 0 | mixed |
| astropy__astropy-14995 | ✅ | ✅ | yes | yes | no | yes | 1 | mixed |
| astropy__astropy-7166 | ✅ | ✅ | yes | no | no | yes | 0 | add_branch |
| astropy__astropy-7336 | ✅ | — | yes | no | yes | yes | 0 | modify_logic |
| astropy__astropy-7606 | ❌ | ❌ | yes | no | yes | yes | 1 | mixed |
| astropy__astropy-7671 | ✅ | ✅ | yes | no | yes | yes | 0 | modify_logic |
| astropy__astropy-8707 | ❌ | ❌ | yes | no | yes | yes | 1 | add_guard |
| astropy__astropy-8872 | ❌ | ❌ | yes | no | yes | yes | 2 | modify_logic |
| django__django-10097 | ❌ | ❌ | yes | no | no | yes | 0 | mixed |
| django__django-10554 | ❌ | ❌ | yes | no | no | no | 1 | mixed |
| django__django-10880 | ✅ | ✅ | yes | no | no | yes | 1 | modify_logic |
| django__django-10914 | ✅ | ✅ | yes | no | no | yes | 0 | modify_logic |
| django__django-10973 | ✅ | ✅ | yes | no | yes | yes | 1 | add_guard |
| django__django-10999 | ❌ | ❌ | yes | no | no | yes | 0 | modify_logic |
| django__django-11066 | ✅ | ✅ | yes | no | yes | yes | 0 | remove_behavior |
| django__django-11087 | ❌ | ❌ | yes | no | yes | no | 1 | modify_logic |

## Table 2 — Outcome comparison

```
HTA wins:    15 / 30  (50.0%)
DAG wins:    15 / 29  (51.7%)   (DAG baseline available on 29 issues)
Both win:    13 / 29
Both fail:   13 / 29
HTA flips:   1 / 29   (HTA ✅ where DAG ❌)
DAG flips:   2 / 29   (DAG ✅ where HTA ❌)
Net flip:    -1  in HTA's favour
```

## Table 3 — Mechanism activation rates

```
RCL discriminates (std > epsilon):                  30 / 30  (100%)
RCL collapses & escalates:                          2 / 30  (7%)
spec_interpretation fires (via escalation):         2 / 30
fix_locality discriminates:                         15 / 30  (50%)
  - sentinel in winner's test_payload:              24 / 30
  - sentinel in any test_payload:                   24 / 30
  - gaming detected (all probes scored 1.0):        1 / 30
IC fires at least once:                             18 / 30
  - via same_file_read_5x (bypass path):            22
  - via meta-judge:                                 0
Novel class hypothesis emitted (any decision):      27 / 30
Tier-2 LLM call used (any decision):                29 / 30   (total calls: 40)
```

## Table 4 — The 4-quadrant question

Each cell shows `HTA wins/total · DAG wins/total` on episodes that fall in that combination of (RCL outcome × patch action family).

| | RCL discriminates | RCL collapses & escalates |
|---|---|---|
| **Patch action: remove-type** | HTA 1/1 · DAG 1/1 | — |
| **Patch action: add-type** | HTA 5/12 · DAG 7/12 | — |
| **Patch action: modify-type** | HTA 6/14 · DAG 5/14 | HTA 2/2 · DAG 2/2 |

## Table 5 — Failure-mode breakdown (HTA losses only)

| Failure category | Count | Example issue |
|---|---|---|
| Gold-test format wall (patch good but bit-exact) | 5 | astropy__astropy-13033 |
| Wrong root cause class (RCL picked badly) | 5 | astropy__astropy-14369 |
| Wrong action type (RCL right, patch did wrong thing) | 1 | astropy__astropy-14096 |
| Mid-execution flail (long stuck, IC didn't help) | 2 | astropy__astropy-14598 |
| Budget exhaustion (ran out before solving) | 0 | — |
| Other | 2 | astropy__astropy-13977 |

## Table 6 — Cost & overhead

Token totals are estimated as iteration_count × 5,000 (placeholder, stated explicitly per spec). DAG iterations are approximated at 50/issue (the README's per-issue range was 31–94, median ~50). Treat ratios as orders of magnitude.

```
Total tokens across 30 issues (HTA, est.):  13,580,000
Total tokens across DAG baseline (est.):            7,250,000
HTA / DAG ratio:                                    1.87
Average decision points per issue:                  2.8
Average IC calls per issue:                         0.73
Average Tier-2 calls per issue:                     1.33  (capped at 3)
Average memory distillations per issue (H3):        2.70  (capped at 6)
```

## Table 7 — Semantic memory accumulation

| Ep | Issue | Decisions | Distillations | Cumulative entries |
|---|---|---|---|---|
| 1 | astropy__astropy-12907 | 4 | 3 | 3 |
| 2 | astropy__astropy-13033 | 4 | 4 | 7 |
| 3 | astropy__astropy-13236 | 3 | 3 | 10 |
| 4 | astropy__astropy-13398 | 2 | 2 | 12 |
| 5 | astropy__astropy-13453 | 3 | 3 | 15 |
| 6 | astropy__astropy-13579 | 4 | 4 | 19 |
| 7 | astropy__astropy-13977 | 4 | 4 | 23 |
| 8 | astropy__astropy-14096 | 3 | 3 | 26 |
| 9 | astropy__astropy-14182 | 3 | 3 | 29 |
| 10 | astropy__astropy-14309 | 2 | 2 | 31 |
| 11 | astropy__astropy-14365 | 3 | 3 | 34 |
| 12 | astropy__astropy-14369 | 2 | 2 | 36 |
| 13 | astropy__astropy-14508 | 2 | 2 | 38 |
| 14 | astropy__astropy-14539 | 3 | 3 | 41 |
| 15 | astropy__astropy-14598 | 1 | 1 | 42 |
| 16 | astropy__astropy-14995 | 5 | 4 | 46 |
| 17 | astropy__astropy-7166 | 2 | 2 | 48 |
| 18 | astropy__astropy-7336 | 2 | 2 | 50 |
| 19 | astropy__astropy-7606 | 3 | 3 | 53 |
| 20 | astropy__astropy-7671 | 2 | 2 | 55 |
| 21 | astropy__astropy-8707 | 3 | 3 | 58 |
| 22 | astropy__astropy-8872 | 4 | 4 | 62 |
| 23 | django__django-10097 | 2 | 2 | 64 |
| 24 | django__django-10554 | 2 | 2 | 66 |
| 25 | django__django-10880 | 3 | 3 | 69 |
| 26 | django__django-10914 | 2 | 2 | 71 |
| 27 | django__django-10973 | 3 | 3 | 74 |
| 28 | django__django-10999 | 2 | 2 | 76 |
| 29 | django__django-11066 | 2 | 2 | 78 |
| 30 | django__django-11087 | 3 | 3 | 81 |
