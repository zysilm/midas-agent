# HTA evaluation report

_Generated from `30` complete HTA episodes and `96` DAG baseline issues._

## Table 1 — Per-episode outcome and mechanism summary

| Issue | HTA | DAG | RCL discriminate | Escalated | fix_loc discriminate | Sentinel used | IC fires | Patch type |
|---|---|---|---|---|---|---|---|---|
| astropy__astropy-12907 | ✅ | ✅ | yes | yes | yes | yes | 1 | modify_logic |
| astropy__astropy-13033 | ❌ | ❌ | yes | no | yes | yes | 2 | add_guard |
| astropy__astropy-13236 | ❌ | ❌ | yes | no | yes | yes | 1 | mixed |
| astropy__astropy-13398 | ❌ | ❌ | yes | yes | no | yes | 0 | add_branch |
| astropy__astropy-13453 | ✅ | ✅ | yes | no | no | yes | 1 | modify_logic |
| astropy__astropy-13579 | ✅ | ✅ | yes | no | no | yes | 1 | add_branch |
| astropy__astropy-13977 | ❌ | ❌ | yes | yes | no | yes | 1 | add_guard |
| astropy__astropy-14096 | ✅ | ✅ | yes | no | no | yes | 0 | add_guard |
| astropy__astropy-14182 | ❌ | ❌ | yes | no | yes | yes | 1 | add_branch |
| astropy__astropy-14309 | ✅ | ✅ | yes | yes | yes | yes | 0 | mixed |
| astropy__astropy-14365 | ❌ | ❌ | yes | no | yes | yes | 1 | modify_logic |
| astropy__astropy-14369 | ✅ | ✅ | yes | no | yes | yes | 1 | mixed |
| astropy__astropy-14508 | ✅ | ✅ | yes | no | no | yes | 1 | add_guard |
| astropy__astropy-14539 | ✅ | ✅ | yes | no | yes | yes | 1 | mixed |
| astropy__astropy-14598 | ❌ | ❌ | yes | no | no | no | 0 | add_branch |
| astropy__astropy-14995 | ✅ | ✅ | yes | no | yes | yes | 1 | mixed |
| astropy__astropy-7166 | ✅ | ✅ | yes | no | yes | yes | 0 | add_branch |
| astropy__astropy-7336 | ✅ | — | yes | no | no | yes | 0 | modify_logic |
| astropy__astropy-7606 | ❌ | ❌ | yes | no | yes | yes | 1 | mixed |
| astropy__astropy-7671 | ✅ | ✅ | yes | no | yes | yes | 1 | modify_logic |
| astropy__astropy-8707 | ❌ | ❌ | yes | no | no | yes | 1 | add_guard |
| astropy__astropy-8872 | ❌ | ❌ | yes | no | no | yes | 2 | modify_logic |
| django__django-10097 | ❌ | ❌ | yes | yes | yes | yes | 1 | modify_logic |
| django__django-10554 | ❌ | ❌ | yes | no | no | no | 0 | mixed |
| django__django-10880 | ❌ | ✅ | yes | no | yes | yes | 0 | mixed |
| django__django-10914 | ❌ | ✅ | yes | no | yes | yes | 1 | mixed |
| django__django-10973 | ✅ | ✅ | yes | no | no | yes | 2 | remove_behavior |
| django__django-10999 | ❌ | ❌ | no | yes | yes | yes | 1 | modify_logic |
| django__django-11066 | ✅ | ✅ | yes | no | no | yes | 0 | modify_logic |
| django__django-11087 | ❌ | ❌ | yes | no | yes | yes | 1 | mixed |

## Table 2 — Outcome comparison

```
HTA wins:    14 / 30  (46.7%)
DAG wins:    15 / 29  (51.7%)   (DAG baseline available on 29 issues)
Both win:    13 / 29
Both fail:   14 / 29
HTA flips:   0 / 29   (HTA ✅ where DAG ❌)
DAG flips:   2 / 29   (DAG ✅ where HTA ❌)
Net flip:    -2  in HTA's favour
```

## Table 3 — Mechanism activation rates

```
RCL discriminates (std > epsilon):                  29 / 30  (97%)
RCL collapses & escalates:                          6 / 30  (20%)
spec_interpretation fires (via escalation):         6 / 30
fix_locality discriminates:                         17 / 30  (57%)
  - sentinel in winner's test_payload:              28 / 30
  - sentinel in any test_payload:                   28 / 30
IC fires at least once:                             21 / 30
  - via same_file_read_5x (bypass path):            24
  - via meta-judge:                                 0
Novel class hypothesis emitted (any decision):      24 / 30
Tier-2 LLM call used (any decision):                27 / 30   (total calls: 52)
```

## Table 4 — The 4-quadrant question

Each cell shows `HTA wins/total · DAG wins/total` on episodes that fall in that combination of (RCL outcome × patch action family).

| | RCL discriminates | RCL collapses & escalates |
|---|---|---|
| **Patch action: remove-type** | HTA 1/1 · DAG 1/1 | — |
| **Patch action: add-type** | HTA 4/8 · DAG 4/8 | HTA 0/2 · DAG 0/2 |
| **Patch action: modify-type** | HTA 6/14 · DAG 8/14 | HTA 2/4 · DAG 2/4 |

## Table 5 — Failure-mode breakdown (HTA losses only)

| Failure category | Count | Example issue |
|---|---|---|
| Gold-test format wall (patch good but bit-exact) | 1 | django__django-10097 |
| Wrong root cause class (RCL picked badly) | 2 | astropy__astropy-13398 |
| Wrong action type (RCL right, patch did wrong thing) | 0 | — |
| Mid-execution flail (long stuck, IC didn't help) | 13 | astropy__astropy-13033 |
| Budget exhaustion (ran out before solving) | 0 | — |
| Other | 0 | — |

## Table 6 — Cost & overhead

Token totals are estimated as iteration_count × 5,000 (placeholder, stated explicitly per spec). DAG iterations are approximated at 50/issue (the README's per-issue range was 31–94, median ~50). Treat ratios as orders of magnitude.

```
Total tokens across 30 issues (HTA, est.):  12,635,000
Total tokens across DAG baseline (est.):            7,250,000
HTA / DAG ratio:                                    1.74
Average decision points per issue:                  3.1
Average IC calls per issue:                         0.80
Average Tier-2 calls per issue:                     1.73  (capped at 3)
```
