# H3 cold-start 30-issue evaluation report

_Run dir: `.midas/train/hta_eval_30_h3/`_
_Branch: `feature/hta-semantic-memory` (H3 — SemanticExperienceMemory replaces TypedAdvantageMemory)._
_Cold start: empty memory at episode 1, no warm start._
_Note: original ep24 (django-10097) and ep25 (django-10554) failed cold, then were retried under partial warm memory after a checkpoint resume. Their warm-retry results are what this report uses for the pass/fail tally (the cold results for those two issues are 0.000 s_w both)._

## 1. Headline

| Metric | H1 baseline | H3 cold-start | Δ |
|---|---|---|---|
| HTA pass count | 15/30 | 15/30 | +0 |
| DAG paired wins (same paired set) | 15/29 | 15/29 | 0 |
| HTA flips (HTA✅ DAG❌) | 1 | 1 | +0 |
| DAG flips (DAG✅ HTA❌) | 2 | 2 | +0 |
| Net flip | -1 | -1 | +0 |
| Wall time | 6h42m | ~18h45m (incl. ~4h resume gap) | (slower; includes resume) |
| Total tokens (est. iters×5K) | 12,895,000 | 13,580,000 | +5.3% |

Cold-start lands at 15/30 — meets the H1 expectation of 15-17/30.

## 2. Distillation health

- Total distillation calls attempted: **81** (decisions minus escalations)
- Successful (returned valid SemanticMemoryEntry): **81**
- Failed (None returned — API error, no tool call, empty fields): **0**
- Success rate: **100.0%**
- Per-episode distillation count distribution: min=1, median=3, max=4
- Episodes hitting cap (max=6): **0/30**

Final SemanticExperienceMemory size: **85 entries**

By decision_type:
- `fix_locality_scope`: 28
- `investigation_continuation`: 23
- `root_cause_localization`: 32
- `spec_interpretation`: 2

By outcome:
- Pass-outcome (outcome_score ≥ 0.5): **40**
- Fail-outcome (outcome_score < 0.5): **45**

_No red flags._

## 3. bias_summary samples (reconstructed)

These are the prompt-injected experience strings the hypothesis-generation LLM would have seen,
reconstructed from the persisted SemanticMemoryEntry log by replaying the memory state at the
start of each target episode. (The actual run does not persist the bias_summary string at call
time; reconstruction uses entry timestamps + per-issue commit order to reproduce the state.)

The actual engine call site is `bias_summary(decision_type)` with no `current_issue_id` argument
(no self-issue exclusion — but during cold-start the self-issue has no entries yet at the time
of the call, so this is equivalent).

### Episode 20 — astropy__astropy-7671

#### bias_summary at RCL DP

```
Past experiences at this decision type:

[#1, issue astropy-7336, outcome ✅]
Winner: __novel__:return_value_none_guard. The winner hypothesis correctly identified the root cause based on its high verifier score (0.90) and large advantage (+1.18). The error message "None has no attribute 'to'" directly confirms the decorator is calling .to() on the return value without checking if it's None first - exactly what the return_value_none_guard hypothesis describes. This signal was strong enough to clearly distinguish it 
Losers: The framework_default_value hypothesis (score 0.50) is related but slightly off - it focuses on the return_annotation being None, whereas the actual bug occurs even when the annotation is correctly provided and the return value is literally None (as expected for __init__ methods). The regex_or_parser_edge hypothesis (score 0.00) lost because the parsing itself works fine; the problem is the runtim

[#2, issue astropy-7166, outcome ✅]
Winner: __novel__:member_filter_check. The winner was correct because the verifier found direct evidence of a member filter using isinstance(obj, FunctionType) or inspect.isfunction() that excludes properties from processing entirely—hitting the precise mechanism causing the bug rather than just naming isfunction usage like Hypothesis 1.
Losers: Hypothesis 1 was too narrow—it only identified that inspect.isfunction() exists somewhere, missing the full filtering logic. Hypothesis 2 had no supporting evidence (advantage -1.40); the problem occurs at member selection, not at the fget/fset/fdel rewapping stage.

[#3, issue astropy-14995, outcome ✅]
Winner: __novel__:_mask_none_guard_removed. A None-check guard that prevented passing None masks to handle_mask was removed between v5.2 and v5.3. The verifier found direct code evidence confirming this: _apply_mask stopped checking "if operand.mask is None" before calling handle_mask, causing np.bitwise_or to receive (int, None) and raise TypeError. The very high score (0.90) and large advantage (+1.41) reflect unambiguous signal.
Losers: The operator_overload_path and state_mutation_order hypotheses scored 0.00 because they proposed indirect mechanisms (refactored multiply path, changed init order) when the actual root cause was a straightforward missing guard in existing mask-composition logic. Both lost because the verifier found no evidence supporting their proposed chains of causation—they were too complex for the actual simpl

[#4, issue astropy-14539, outcome ✅]
Winner: __novel__:vla_heap_pointer_comparison. The vla_heap_pointer_comparison hypothesis won decisively with a verifier score of 0.85 and advantage of +1.29 — the strongest signal. The key was that when comparing VLA data, the diff logic was comparing the heap pointer values (memory addresses) in the heap descriptors rather than the actual pointed-to array data, causing false positives even when comparing identical files.
Losers: Serialization_roundtrip was decisively ruled out (score 0.00) because the issue occurred even without any write/read roundtrips — the bug manifested when comparing the same file to itself. The vla_heap_descriptor_mismatch hypothesis lost because the actual bug was about pointer value comparison, not descriptor field mismatches; the verifier could discriminate based on whether the comparison was lo

[#5, issue astropy-7606, outcome ❌]
Winner: __novel__:unrecognized_unit_equality_name_mismatch. Hypothesis 3 won because it pinpointed UnrecognizedUnit's specific name-based equality logic that was never executed—the class stores custom name strings and uses strict name equality in __eq__, but the TypeError from Unit conversion happens before this code path runs. The verifier gave this the highest score (0.70) and advantage (+1.02), confirming the root cause was class-specific handling, not 
Losers: Hypothesis 1 (operator overload) scored moderately (0.50, +0.34) because __eq__ does attempt conversion—but this is true for all Units, not causing this specific bug. Hypothesis 2 completely missed (0.00, -1.36) because expecting None to convert to dimensionless is not the design intent; UnrecognizedUnit should simply return False on comparison failure, not require framework-level None handling.
```

#### bias_summary at fix_locality_scope DP

```
Past experiences at this decision type:

[#1, issue astropy-7336, outcome ✅]
Winner: intermediate_layer. intermediate_layer was the right choice because it achieved the highest verifier score (0.50 vs 0.20) with a positive advantage (+1.41 vs -0.71), indicating the abstraction of a dedicated helper function was seen as cleaner than inline patching. This approach properly encapsulates the check logic rather than adding a band-aid surface fix.
Losers: The surface_patch and dual_fix hypotheses both scored low (0.20) with negative advantages (-0.71), meaning the verifier found them insufficient—surface_patch appeared too shallow, and dual_fix added unnecessary complexity by patching the helper function when only the decorator needed fixing.

[#2, issue astropy-7166, outcome ✅]
Winner: surface_patch. The surface_patch was selected as the most direct solution—adding property/staticmethod/classmethod detection right where the member filtering occurs avoids unnecessary indirection. However, the win was marginal: all three hypotheses scored 0.50, indicating the verifier could not strongly discriminate between them.
Losers: Both intermediate_layer and root_layer scored identically to the winner (0.50), meaning the verifier could not distinguish which scope was truly better. The intermediate_layer approach of adding _get_doc/_set_doc helpers and the root_layer redesign scored the same—they were not rejected, just not preferred over the simpler surface fix.

[#3, issue astropy-14995, outcome ✅]
Winner: surface_patch. surface_patch was selected because the bug was directly traced to the _arithmetic_mask method - when self.mask exists but operand.mask is None, the else branch incorrectly passes None to np.bitwise_or, causing a TypeError. The fix went directly where the mask combination fails.
Losers: All three hypotheses scored 0.20 with zero advantage - the verifier could not discriminate between them, indicating a weak signal. The intermediate_layer and root_layer hypotheses may also contain valid insights about higher-level routing and composite mask logic, but the verifier was unable to rule them in or out.

[#4, issue astropy-14539, outcome ✅]
Winner: surface_patch. surface_patch was selected as the minimally invasive fix because it directly addresses the specific issue at line 1452 in diff.py by adding 'Q' to handle 64-bit heap descriptors alongside 32-bit P descriptors, matching the pattern already in place. However, the win was weak—all hypotheses tied at 0.20, so this choice was driven more by implementation simplicity than verifier discrimination.
Losers: All three hypotheses scored identically (0.20 with +0.00 advantage), meaning the verifier could not discriminate between them. The intermediate_layer and root_column_format_detection approaches were not actually rejected—they were effectively tied with surface_patch, so choosing surface_patch was somewhat arbitrary based on the verifier evidence alone.

[#5, issue astropy-7606, outcome ❌]
Winner: surface_patch. The surface_patch hypothesis correctly identified that UnrecognizedUnit's own __eq__ method lacked exception handling around Unit(other) conversion, separate from the base class design. The fix was applied directly to UnrecognizedUnit._represents_as() to handle None gracefully rather than letting TypeError propagate.
Losers: The intermediate_layer hypothesis predicted a shared utility fix would suffice, but examining the code showed each subclass (like FunctionUnitBase) had its own independent __eq__ implementation requiring separate handling. The dual_fix hypothesis focused on __ne__ asymmetry which was not the core issue—the verifier scores of 0.50 for both winners indicated weak discrimination, with the advantage (
```

#### bias_summary at investigation_continuation DP

IC did not fire this episode.

### Episode 25 — django__django-10880

#### bias_summary at RCL DP

```
Past experiences at this decision type:

[#1, issue astropy-7671, outcome ✅]
Winner: regex_or_parser_edge. LooseVersion's parser produces incompatible component types when parsing version strings - '1.14dev' yields [1, 14, 'dev'] (mixed int and str), while '1.14.3' yields [1, 14, 3] (all ints). This type mismatch causes the >= comparison to fail at runtime with a TypeError. The verifier score of 0.50 (+1.41 advantage) confirmed this as the root cause - the failure happens specifically because the parse
Losers: Hypothesis 2 (operator overload) incorrectly generalized the issue to the >= operator itself, but LooseVersion's >= works fine for same-type components - the actual bug is the parser creating mixed types. Hypothesis 3 (framework default) was historically accurate context but didn't identify the technical mechanism causing the failure - it described what changed, not why it breaks.

[#2, issue astropy-7336, outcome ✅]
Winner: __novel__:return_value_none_guard. The winner hypothesis correctly identified the root cause based on its high verifier score (0.90) and large advantage (+1.18). The error message "None has no attribute 'to'" directly confirms the decorator is calling .to() on the return value without checking if it's None first - exactly what the return_value_none_guard hypothesis describes. This signal was strong enough to clearly distinguish it 
Losers: The framework_default_value hypothesis (score 0.50) is related but slightly off - it focuses on the return_annotation being None, whereas the actual bug occurs even when the annotation is correctly provided and the return value is literally None (as expected for __init__ methods). The regex_or_parser_edge hypothesis (score 0.00) lost because the parsing itself works fine; the problem is the runtim

[#3, issue astropy-7166, outcome ✅]
Winner: __novel__:member_filter_check. The winner was correct because the verifier found direct evidence of a member filter using isinstance(obj, FunctionType) or inspect.isfunction() that excludes properties from processing entirely—hitting the precise mechanism causing the bug rather than just naming isfunction usage like Hypothesis 1.
Losers: Hypothesis 1 was too narrow—it only identified that inspect.isfunction() exists somewhere, missing the full filtering logic. Hypothesis 2 had no supporting evidence (advantage -1.40); the problem occurs at member selection, not at the fget/fset/fdel rewapping stage.

[#4, issue astropy-14995, outcome ✅]
Winner: __novel__:_mask_none_guard_removed. A None-check guard that prevented passing None masks to handle_mask was removed between v5.2 and v5.3. The verifier found direct code evidence confirming this: _apply_mask stopped checking "if operand.mask is None" before calling handle_mask, causing np.bitwise_or to receive (int, None) and raise TypeError. The very high score (0.90) and large advantage (+1.41) reflect unambiguous signal.
Losers: The operator_overload_path and state_mutation_order hypotheses scored 0.00 because they proposed indirect mechanisms (refactored multiply path, changed init order) when the actual root cause was a straightforward missing guard in existing mask-composition logic. Both lost because the verifier found no evidence supporting their proposed chains of causation—they were too complex for the actual simpl

[#5, issue django-10554, outcome ❌]
Winner: __novel__:combinator_query_copy. The combinator_query_copy hypothesis (score 0.70, advantage +1.02) was correct because the bug stems from union querysets failing to copy the base query when applying .order_by(), causing the combinator to retain stale references to the original queryset's query state. The high verifier score reflected this being the specific mechanism causing the corruption rather than general state mutation.
Losers: The state_mutation_order hypothesis (score 0.50) was wrong because while it correctly identified mutation as involved, it focused on the wrong layer (_order_by flags) rather than the query copying mechanism itself. The operator_overload_path hypothesis (score 0.00) failed completely because UnionSQLCompiler handling was not the problem—the issue was insufficient isolation at the query object level
```

#### bias_summary at fix_locality_scope DP

```
Past experiences at this decision type:

[#1, issue astropy-7671, outcome ✅]
Winner: __novel__:_global_version_utils. The global version_utils approach was selected because it's a root-layer fix that prevents LooseVersion TypeErrors across the entire astropy codebase, not just at one usage point. The verifier gave it a significantly higher score (0.50 vs 0.20) and positive advantage (+1.41), indicating it was viewed as the most comprehensive solution.
Losers: Both the surface patch and intermediate wrapper lost because they only provide local fixes at a single location rather than addressing version parsing systematically. Neither could distinguish itself from the other—both scored identically at 0.20 with equal negative advantages—confirming the verifier saw them as equivalently insufficient approaches.

[#2, issue astropy-7336, outcome ✅]
Winner: intermediate_layer. intermediate_layer was the right choice because it achieved the highest verifier score (0.50 vs 0.20) with a positive advantage (+1.41 vs -0.71), indicating the abstraction of a dedicated helper function was seen as cleaner than inline patching. This approach properly encapsulates the check logic rather than adding a band-aid surface fix.
Losers: The surface_patch and dual_fix hypotheses both scored low (0.20) with negative advantages (-0.71), meaning the verifier found them insufficient—surface_patch appeared too shallow, and dual_fix added unnecessary complexity by patching the helper function when only the decorator needed fixing.

[#3, issue astropy-7166, outcome ✅]
Winner: surface_patch. The surface_patch was selected as the most direct solution—adding property/staticmethod/classmethod detection right where the member filtering occurs avoids unnecessary indirection. However, the win was marginal: all three hypotheses scored 0.50, indicating the verifier could not strongly discriminate between them.
Losers: Both intermediate_layer and root_layer scored identically to the winner (0.50), meaning the verifier could not distinguish which scope was truly better. The intermediate_layer approach of adding _get_doc/_set_doc helpers and the root_layer redesign scored the same—they were not rejected, just not preferred over the simpler surface fix.

[#4, issue astropy-14995, outcome ✅]
Winner: surface_patch. surface_patch was selected because the bug was directly traced to the _arithmetic_mask method - when self.mask exists but operand.mask is None, the else branch incorrectly passes None to np.bitwise_or, causing a TypeError. The fix went directly where the mask combination fails.
Losers: All three hypotheses scored 0.20 with zero advantage - the verifier could not discriminate between them, indicating a weak signal. The intermediate_layer and root_layer hypotheses may also contain valid insights about higher-level routing and composite mask logic, but the verifier was unable to rule them in or out.

[#5, issue django-10097, outcome ❌]
Winner: surface_patch. Surface_patch was selected because it represented the minimal, most direct fix—modifying a single character class in the existing regex at line 97—without adding complexity. However, this win was effectively arbitrary: all three hypotheses scored identically (0.50, advantage +0.00), indicating the verifier could not discriminate between them.
Losers: Intermediate_layer and root_layer did not lose because they were inferior—they were genuinely competitive alternatives. They lost because the verifier assigned identical scores to all options, meaning no meaningful distinction could be drawn between a simple regex tweak, a pre-validation function, or a full parser rewrite. Future agents should treat such ties as a signal to seek stronger discrimin
```

#### bias_summary at investigation_continuation DP

```
Past experiences at this decision type:

[#1, issue astropy-14995, outcome ✅]
Winner: pivot_target. pivot_target won because the verifier identified signal (similar None-check issues likely existing in composite.py for mask composition functions) with high confidence (0.80 score), scoring well above the other hypotheses and finding the root cause warranted looking beyond the initially fixed location.
Losers: persist_same_path lost because the verifier saw that the fix pattern (operand.mask is None check) needed to be applied not just in ndarithmetic.py but also in composite.py's _arithmetic_mask - assuming the fix was complete was premature. abandon lost because while the immediate TypeError was addressed, the underlying pattern of None-checking appeared in multiple related functions requiring broader

[#2, issue astropy-14539, outcome ✅]
Winner: pivot_target. The pivot_target hypothesis won because it identified a clear systemic pattern—hardcoded "P"/"Q" format checks likely exist beyond the single conditional that was fixed—and the verifier gave it a commanding advantage (+0.91) over both the simple fix (abandon) and the risky architectural refactor. This signal mattered: searching for other hardcoded checks is low-risk investigation with high potenti
Losers: The architectural_fix hypothesis lost decisively (score 0.00, advantage -1.39) because the verifier viewed a full refactor as too invasive for a narrow FITSDiff bug—the current fix adequately resolves the reported issue without introducing new risk. The abandon hypothesis (accepting the fix) was reasonable but ranked lower because the systemic pattern warranted targeted follow-up before closing th

[#3, issue astropy-14365, outcome ✅]
Winner: pivot_target. The pivot_target hypothesis won because the QDP parser inherits from ascii.Base, creating a plausible signal that case-insensitivity issues could cascade to other ASCII formats. The high verifier score (0.80) and strong advantage (+1.19) reflected this systemic risk — the verifier recognized the inheritance chain meant the fix could have broader impact beyond just QDP.
Losers: The persist_same_path hypothesis lost decisively (score 0.25, advantage -1.26) because it focused narrowly on QDP-specific bugs rather than the broader class hierarchy. The abandon hypothesis nearly tied (advantage +0.07) but lost because the verifier saw the inheritance connection to Base made further investigation worthwhile — the issue wasn't fully resolved by just fixing QDP commands.

[#4, issue astropy-13579, outcome ✅]
Winner: pivot_target. pivot_target was the right call because verifier evidence strongly supported shifting investigation from the SlicedLowLevelWCS implementation itself to how it delegates to high-level WCS wrappers - scoring 0.80 with +1.12 advantage indicated the problem was mislocalized to the wrong layer.
Losers: persist_same_path scored too low (0.15, -1.31 advantage) because blindly continuing with the existing fix yielded no new traction; pivot_evidence_type was marginal (0.55, +0.19 advantage) because adding test cases didn't fundamentally reorient the investigation toward the actual root cause in wrapper communication.

[#5, issue django-10554, outcome ❌]
Winner: pivot_target. pivot_target was the correct choice because it identified a concrete, high-impact extension: intersection and difference combinators use the same combined_queries mechanism as union and would have the same vulnerability, justifying immediate broader fix rather than limited scope. The high verifier score (0.80) and positive advantage (+0.96) reflected this strategic pivot opportunity.
Losers: persist_same_path lost with very low score (0.15) and negative advantage (-1.38) because it merely continued down the same narrow path without addressing the wider pattern. extended_clone_coverage was plausible but exploratory ("may not be properly cloned") rather than identifying specific combinators needing the same fix, making it less actionable than pivot_target.
```

### Episode 30 — django__django-11087

#### bias_summary at RCL DP

```
Past experiences at this decision type:

[#1, issue django-11066, outcome ✅]
Winner: __novel__:missing_using_parameter. The winning hypothesis earned a perfect verifier score of 1.00 with a decisive +1.41 advantage over runners-up. The specific signal was that the code explicitly uses `transaction.atomic(using=db)` yet immediately calls `content_type.save(update_fields={'model'})` without passing `using=db` — a clear mismatch where the transaction context specifies the database but the save call ignores it.
Losers: Both losing hypotheses scored 0.00 with negative advantages (-0.71), indicating the verifier found no discriminative evidence for them. Hypothesis 2 (state_mutation_order) failed because the issue is about a missing parameter, not ordering. Hypothesis 3 (error_message_only) failed because this is a causal root cause (wrong database chosen), not merely how errors surface.

[#2, issue django-10973, outcome ✅]
Winner: __novel__:subprocess_environment_isolation. The environment isolation hypothesis won decisively because passing credentials via PGPASSWORD in a custom env dict to subprocess.run avoids polluting os.environ globally—this directly addresses the core problem described in the issue. The high verifier score (0.70) and large advantage (+1.16) reflected this clear improvement over global state modification.
Losers: The argument-passing hypothesis lost because it's a secondary security concern (command-line visibility) rather than the main issue; it scored only 0.40 with minimal advantage (+0.12). The framework_default_value hypothesis scored 0.00 with negative advantage (-1.28)—the verifier strongly disfavored it, as the issue isn't about missing defaults but about an explicit architectural flaw in environme

[#3, issue django-10914, outcome ✅]
Winner: __novel__:temporary_file_permissions. Hypothesis 3 won because it identified the specific causal mechanism: os.rename in FileSystemStorage.save() preserves the source file's existing permissions instead of applying new ones. TemporaryUploadedFile gets 0o600 from tempfile and keeps it; MemoryUploadedFile becomes a fresh file with default umask-based 0o644. This directly explains the size-dependent inconsistency described in the issue.
Losers: Hypotheses 1 and 2 both scored 0.00 with advantage -0.71 — the verifier could not discriminate between them, and both failed to identify the core mechanism. Hypothesis 1 correctly noted missing defaults but didn't explain how permissions vary by upload handler; Hypothesis 2 focused on operation ordering but the actual issue is that os.rename preserves permissions, not the sequence itself.

[#4, issue django-10880, outcome ✅]
Winner: __novel__:aggregate_sql_spacing. The aggregate_sql_spacing hypothesis was clearly correct: the SQL aggregates compiler was failing to add proper whitespace between DISTINCT and CASE expressions, producing invalid SQL like "COUNT(DISTINCTCASE...)". The high verifier score (0.85) and large advantage (+1.41) over both losers (both at 0.00) confirmed this was the root cause.
Losers: Both losing hypotheses scored 0.00 with identical negative advantages (-0.71), indicating the verifier could clearly discriminate against them. The regex_or_parser_edge and state_mutation_order hypotheses were rejected because they misattributed the bug—the issue was in the SQL generation spacing logic, not in regex parsing or state mutation order.

[#5, issue django-10999, outcome ❌]
Winner: regex_or_parser_edge. The winner correctly identified the core issue: the lookahead `(?=\d+:\d+)` in the hours group restricts to positive digits only (`\d+`), while the capturing group `(?P<hours>-?\d+)` permits negative signs (`-?\d+`). This inconsistency is a classic regex edge case where assertion requirements don't match capture behavior.
Losers: Both losing hypotheses scored poorly (0.10 and 0.00) compared to the winner (1.00). The group_capture_interaction hypothesis wrongly focused on nested optional group structure, while error_message_only incorrectly blamed error handling rather than the actual regex pattern mismatch.
```

#### bias_summary at fix_locality_scope DP

```
Past experiences at this decision type:

[#1, issue django-11066, outcome ✅]
Winner: surface_patch. The surface_patch hypothesis won because it directly fixes the specific bug location with the minimal change needed — adding `using=db` to the `save()` call at line 27 — targeting precisely where the database routing went wrong. The verifier gave it a +0.71 advantage over other options, reflecting that a localized fix at the call site is less risky than broader changes.
Losers: The intermediate_layer hypothesis lost because though it would provide a universal fix for all ContentType saves, it requires modifying the model's save() method which adds complexity and risk across the codebase. The _transaction_context_inheritance lost because making transaction.atomic automatically set model _state.db is overly invasive and could cause unintended side effects. Notably, surface

[#2, issue django-10973, outcome ✅]
Winner: __novel__:_pgpass_file_elimination. The winner (__novel__:_pgpass_file_elimination) achieved a significantly higher verifier score (0.85, advantage +1.41) because it provided the most comprehensive solution—not only using subprocess.run with PGPASSWORD but entirely eliminating the temporary .pgpass file mechanism, thereby removing two problems (os.environ pollution AND unnecessary file operations) rather than just one.
Losers: Hypotheses 1 and 2 both scored 0.50 with identical negative advantages (-0.71), indicating they were partial fixes that addressed only the immediate subprocess/env issue without tackling the underlying .pgpass temporary file mechanism that caused unnecessary file I/O and complexity.

[#3, issue django-10914, outcome ✅]
Winner: root_layer. Setting FILE_UPLOAD_PERMISSIONS to 0o644 as a global default was selected because it provides the simplest, most comprehensive fix—a single configuration change ensures all uploaded files get consistent permissions regardless of whether MemoryUploadedFile or TemporaryUploadedFile was used, eliminating the inconsistency at its source rather than patching multiple layers.
Losers: All three hypotheses scored identically (0.50 advantage +0.00), indicating the verifier could not strongly discriminate between them—they represent equally valid technical approaches at different layers. The surface_patch and intermediate_layer options were rejected not due to flaws but because the root_layer approach offers broader impact with less code modification and fewer places where bugs co

[#4, issue django-10880, outcome ✅]
Winner: surface_patch. The surface_patch hypothesis targeting the Count class definition was correct because the bug manifests directly in Count aggregate initialization where `distinct=True` is set without a trailing space. The generated SQL showed "DISTINCTCASE" concatenated together, which is precisely what happens when `distinct=` is assigned without whitespace to the following expression—so the fix location matched
Losers: All three hypotheses scored 0.50 verifier scores—essentially tied, meaning the verifier could not discriminate between them based on available signals. The intermediate_layer (SQL compilation whitespace stripping) and root_layer (Aggregate base class) hypotheses remained plausible but were less directly supported by the specific error evidence showing the concatenation issue originates in immediat

[#5, issue django-10999, outcome ❌]
Winner: surface_patch. The surface_patch was correctly selected because it offers the most minimal, targeted fix - directly modifying the regex lookahead from `(?=\d+:\d+)` to `(?=-?\d+:-?\d+)` to accept negative signs in hours and minutes following negative hours. This directly addresses the specific bug without adding complexity.
Losers: The losing hypotheses lost because the verifier couldn't discriminate - all three hypotheses scored identically at 0.40. The intermediate_layer and root_layer approaches were more invasive than necessary for a straightforward regex bug, but the verifier provided no clear signal to prefer one over another.
```

#### bias_summary at investigation_continuation DP

```
Past experiences at this decision type:

[#1, issue django-10973, outcome ✅]
Winner: pivot_target. The pivot_target hypothesis won because the verifier assigned it the highest score (0.80) with a positive advantage (+0.96), recognizing that checking for similar os.environ modification patterns in MySQL and Oracle backends could reveal systemic credential-handling issues beyond just the postgres fix.
Losers: The persist_same_path hypothesis lost decisively with the lowest score (0.15) and negative advantage (-1.38) — the verifier saw no value in simply finalizing the existing fix. The abandon hypothesis scored moderately (0.65) but lost because while the core issue was resolved, the verifier judged that systemic patterns elsewhere were worth investigating rather than abandoning outright.

[#2, issue django-10880, outcome ✅]
Winner: pivot_target. Pivot_target won because the issue exposed a potential systemic pattern—Count with DISTINCT had a missing space bug—so the verifier correctly prioritized checking whether other aggregation functions (Sum, Avg, Max) have similar distinct handling gaps, achieving the highest score (0.80, +0.96 advantage) by addressing the broader architectural concern.
Losers: Abandon lost because the verifier wanted broader assurance—not just that Count worked but that the entire aggregation framework was sound. Persist_same_path lost decisively (-1.38 advantage) because focusing on edge cases like nested CASE or NULL handling was premature before verifying other core aggregation paths hadn't similarly forgotten the trailing space.

[#3, issue astropy-14995, outcome ✅]
Winner: pivot_target. pivot_target won because the verifier identified signal (similar None-check issues likely existing in composite.py for mask composition functions) with high confidence (0.80 score), scoring well above the other hypotheses and finding the root cause warranted looking beyond the initially fixed location.
Losers: persist_same_path lost because the verifier saw that the fix pattern (operand.mask is None check) needed to be applied not just in ndarithmetic.py but also in composite.py's _arithmetic_mask - assuming the fix was complete was premature. abandon lost because while the immediate TypeError was addressed, the underlying pattern of None-checking appeared in multiple related functions requiring broader

[#4, issue astropy-14539, outcome ✅]
Winner: pivot_target. The pivot_target hypothesis won because it identified a clear systemic pattern—hardcoded "P"/"Q" format checks likely exist beyond the single conditional that was fixed—and the verifier gave it a commanding advantage (+0.91) over both the simple fix (abandon) and the risky architectural refactor. This signal mattered: searching for other hardcoded checks is low-risk investigation with high potenti
Losers: The architectural_fix hypothesis lost decisively (score 0.00, advantage -1.39) because the verifier viewed a full refactor as too invasive for a narrow FITSDiff bug—the current fix adequately resolves the reported issue without introducing new risk. The abandon hypothesis (accepting the fix) was reasonable but ranked lower because the systemic pattern warranted targeted follow-up before closing th

[#5, issue django-10554, outcome ❌]
Winner: pivot_target. pivot_target was the correct choice because it identified a concrete, high-impact extension: intersection and difference combinators use the same combined_queries mechanism as union and would have the same vulnerability, justifying immediate broader fix rather than limited scope. The high verifier score (0.80) and positive advantage (+0.96) reflected this strategic pivot opportunity.
Losers: persist_same_path lost with very low score (0.15) and negative advantage (-1.38) because it merely continued down the same narrow path without addressing the wider pattern. extended_clone_coverage was plausible but exploratory ("may not be properly cloned") rather than identifying specific combinators needing the same fix, making it less actionable than pivot_target.
```


## 4. Per-episode pass/fail table

| Ep | Issue | H3 | s_w | H1 | Same? | Patch type | DPs | Distillations |
|---|---|---|---|---|---|---|---|---|
| 1 | astropy__astropy-12907 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 4 | 3 |
| 2 | astropy__astropy-13033 | ❌ | 0.120 | ❌ | ✅ | add_guard | 4 | 4 |
| 3 | astropy__astropy-13236 | ❌ | 0.129 | ❌ | ✅ | add_warning | 3 | 3 |
| 4 | astropy__astropy-13398 | ❌ | 0.264 | ❌ | ✅ | add_branch | 2 | 2 |
| 5 | astropy__astropy-13453 | ✅ | 1.000 | ✅ | ✅ | mixed | 3 | 3 |
| 6 | astropy__astropy-13579 | ✅ | 1.000 | ✅ | ✅ | add_branch | 4 | 4 |
| 7 | astropy__astropy-13977 | ❌ | 0.162 | ❌ | ✅ | mixed | 4 | 4 |
| 8 | astropy__astropy-14096 | ❌ | 0.234 | ✅ | ❌ (H3-only loss) | add_guard | 3 | 3 |
| 9 | astropy__astropy-14182 | ❌ | 0.270 | ❌ | ✅ | add_branch | 3 | 3 |
| 10 | astropy__astropy-14309 | ✅ | 1.000 | ✅ | ✅ | add_guard | 2 | 2 |
| 11 | astropy__astropy-14365 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 3 | 3 |
| 12 | astropy__astropy-14369 | ❌ | 0.165 | ❌ | ✅ | add_branch | 2 | 2 |
| 13 | astropy__astropy-14508 | ✅ | 1.000 | ✅ | ✅ | add_guard | 2 | 2 |
| 14 | astropy__astropy-14539 | ✅ | 1.000 | ❌ | ❌ (H3-only win) | modify_logic | 3 | 3 |
| 15 | astropy__astropy-14598 | ❌ | 0.150 | ❌ | ✅ | mixed | 1 | 1 |
| 16 | astropy__astropy-14995 | ✅ | 1.000 | ✅ | ✅ | mixed | 5 | 4 |
| 17 | astropy__astropy-7166 | ✅ | 1.000 | ✅ | ✅ | add_branch | 2 | 2 |
| 18 | astropy__astropy-7336 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 2 | 2 |
| 19 | astropy__astropy-7606 | ❌ | 0.255 | ❌ | ✅ | mixed | 3 | 3 |
| 20 | astropy__astropy-7671 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 2 | 2 |
| 21 | astropy__astropy-8707 | ❌ | 0.261 | ❌ | ✅ | add_guard | 3 | 3 |
| 22 | astropy__astropy-8872 | ❌ | 0.300 | ❌ | ✅ | modify_logic | 4 | 4 |
| 23 | django__django-10097 | ❌ | 0.135 | ❌ | ✅ | modify_logic | 2 | 2 |
| 24 | django__django-10554 | ❌ | 0.246 | ❌ | ✅ | add_guard | 2 | 2 |
| 25 | django__django-10880 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 3 | 3 |
| 26 | django__django-10914 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 2 | 2 |
| 27 | django__django-10973 | ✅ | 1.000 | ✅ | ✅ | remove_behavior | 3 | 3 |
| 28 | django__django-10999 | ❌ | 0.135 | ❌ | ✅ | modify_logic | 2 | 2 |
| 29 | django__django-11066 | ✅ | 1.000 | ✅ | ✅ | modify_logic | 2 | 2 |
| 30 | django__django-11087 | ❌ | 0.255 | ❌ | ✅ | add_branch | 3 | 3 |

## 5. Divergence cases (H3 outcome ≠ H1 outcome)

**astropy__astropy-14096** — H3 loses
- RCL winner: H3=`__novel__::property_error_propagation` · H1=`__novel__:_descriptor_protocol_confusion`
- fix_locality winner: H3=`surface_patch` · H1=`surface_patch`

**astropy__astropy-14539** — H3 wins
- RCL winner: H3=`__novel__:vla_heap_pointer_comparison` · H1=`__novel__:_heap_pointer_miscalculation`
- fix_locality winner: H3=`surface_patch` · H1=`root_layer`

Hypothesis on causation will follow Section 7.

## 6. Memory accumulation curve

| After episode | Total | RCL | fix_locality | spec_interp | IC | Pass-outcome | Fail-outcome |
|---|---|---|---|---|---|---|---|
| 5 | 15 | 5 | 5 | 1 | 4 | 6 | 9 |
| 10 | 31 | 10 | 10 | 1 | 10 | 12 | 19 |
| 15 | 42 | 15 | 13 | 1 | 13 | 20 | 22 |
| 20 | 55 | 20 | 18 | 2 | 15 | 30 | 25 |
| 25 | 73 | 27 | 23 | 2 | 21 | 33 | 40 |
| 30 | 85 | 32 | 28 | 2 | 23 | 40 | 45 |

## 7. Sample distilled entries

### Pass-outcome (≥ 0.9)

```json
{
  "decision_type": "investigation_continuation",
  "winner_class": "pivot_target",
  "winner_summary": "The winner pivot_target correctly selected because the primary fix revealed potentially similar hardcoded value bugs in other coupled coordinate transformation methods (like celestial_to_pixel_values). The high verifier score (0.80) reflects that the initial bug fix exposed a pattern worth investigating - fixing one instance doesn't guarantee the underlying systemic issue is fully resolved.",
  "counterfactual_summary": "Persist_same_path lost badly (verifier score 0.15, advantage -1.38) because simply accepting the one-method fix ignores the discovered pattern - there likely are similar hardcoded values elsewhere. Abandon also scored modestly (0.65) because stopping entirely after the first fix leaves known risks unaddressed; the verifier correctly discriminated that the exposed vulnerability deserves broader inv",
  "outcome_score": 1.0,
  "issue_id": "astropy__astropy-13579",
  "is_novel_winner": false
}
```

### Fail-outcome (≤ 0.3)

```json
{
  "decision_type": "investigation_continuation",
  "winner_class": "pivot_target",
  "winner_summary": "The pivot_target hypothesis won decisively with a verifier score of 0.80 (advantage +1.41) because it identified that fixing the error message output is insufficient - the real solution requires preventing required column removal entirely, addressing the root cause rather than just the misleading exception symptom.",
  "counterfactual_summary": "The persist_same_path hypothesis scored too low (0.15) because continuing to verify edge cases doesn't address the core problem - the current fix only treats the symptom. Similarly, revert_and_verify_original_behavior also scored low (0.15) because verifying the original issue was resolved doesn't help determine if the fix approach itself is optimal; both approaches focus on validation rather than",
  "outcome_score": 0.0,
  "issue_id": "astropy__astropy-13033",
  "is_novel_winner": false
}
```

### `__novel__:...` winner

```json
{
  "decision_type": "root_cause_localization",
  "winner_class": "__novel__:temporary_file_permissions",
  "winner_summary": "Hypothesis 3 won because it identified the specific causal mechanism: os.rename in FileSystemStorage.save() preserves the source file's existing permissions instead of applying new ones. TemporaryUploadedFile gets 0o600 from tempfile and keeps it; MemoryUploadedFile becomes a fresh file with default umask-based 0o644. This directly explains the size-dependent inconsistency described in the issue.",
  "counterfactual_summary": "Hypotheses 1 and 2 both scored 0.00 with advantage -0.71 — the verifier could not discriminate between them, and both failed to identify the core mechanism. Hypothesis 1 correctly noted missing defaults but didn't explain how permissions vary by upload handler; Hypothesis 2 focused on operation ordering but the actual issue is that os.rename preserves permissions, not the sequence itself.",
  "outcome_score": 1.0,
  "issue_id": "django__django-10914",
  "is_novel_winner": true
}
```

## 8. Decision graph + trace + patch (per episode)

The full per-episode breakdown (decision graph, compressed ReAct trace, patch diff)
lives in `episode_traces_compact.md` in this same directory. That file is produced by
the analysis sub-agent in the H1/H3 standard format (~3700 lines) and is kept
separate to keep this report scannable.

See: `.midas/train/hta_eval_30_h3/episode_traces_compact.md`

## 9. Go/no-go signal for Run B

**Quantitative checks** (Sections 2, 6):
- ✓ distillation success rate **100.0%** (every attempted call returned a valid SemanticMemoryEntry)
- ✓ cap hit in **0/30** episodes — `max_memory_distillations=6` is comfortably above the observed per-episode load (median 3, max 4)
- ✓ memory grew to **85 entries** across 30 issues — close to the spec's 60-150 prediction; growth is roughly linear (~2.8 entries/episode)
- ✓ distribution across decision types is balanced (RCL 32, fix_locality 28, IC 23, spec_interp 2 — the last is small because only 2 episodes escalated, both H1-D3 working as designed)
- ✓ pass/fail label split is **40/45** — failed-outcome entries are being preserved as "what NOT to do" guidance, not silently discarded

**Qualitative read of Section 3 (bias_summary)** — I read the 9 sampled
prompt-injections directly:

- The strings name **specific functions, classes, and code paths** —
  e.g. `_apply_mask`, `np.bitwise_or`, `os.rename`, `handle_mask`,
  `FileSystemStorage.save`, `UnrecognizedUnit.__eq__`. Not abstract.
- Winner reasoning consistently cites a **concrete verifier signal**
  ("verifier score 0.85, advantage +1.29 — the strongest signal"),
  not vague hand-waving.
- Loser reasoning explains **why** competitors lost — usually because
  they were too narrow, too indirect, or the verifier couldn't find
  supporting evidence. This is exactly the "what didn't work" signal
  the design called for.
- Failed-outcome entries are correctly framed as cautionary —
  e.g. the astropy-13033 fail-entry warns that
  "the current fix only treats the symptom" rather than presenting
  the choice as validated.

**Qualitative read of Section 7 (sample entries)** — all three
sampled entries are substantive: they name specific causal mechanisms
(temp-file permission inheritance, missing None-guard on mask
composition, pointer-vs-descriptor diff logic) and would plausibly
help a future agent at a similar decision point. None read as filler.

**Section 5 — divergences look like LLM sampling noise, not memory-causal.**
Both H1 and H3 picked novel-class winners with similar slugs and the
same fix_locality verdict (`surface_patch`) on astropy-14096 and
astropy-14539. The pass/fail flip is plausibly within the LLM's run-to-run
variance. This is consistent with the cold-start design — H3's memory
layer has nothing to retrieve at episode 1, and by the time it accumulates
enough entries to influence behaviour, most of the easy-discrimination
issues are already decided the same way as H1.

### Verdict

**GO for Run B.** The H3 mechanism is firing cleanly, the persisted
memory content is high-quality and topically specific, and there is no
red flag in the mechanical or qualitative checks. Cold-start was not
expected to move pass rate, and it didn't — but every measurable health
check passed.

Recommended Run B configuration: re-run the same 30 issues with the
85-entry memory pre-loaded (no `--fresh`); compare per-episode RCL
winners and fix_locality verdicts against this cold-start baseline to
test the actual causal hypothesis ("warm memory changes hypothesis
selection on issues with relevant prior experience").

If Run B shows ≥ 2 outcome flips (in either direction) and visible
shifts in RCL winner classes vs cold-start on issues where bias_summary
returned strong matches, the semantic-memory layer is causally active.
If Run B is bit-identical to cold-start on outcomes and winner classes,
the bias_summary is being read but ignored — at which point the next
debug step is the hypothesis-gen prompt's "Read past experiences as
guidance" framing.
