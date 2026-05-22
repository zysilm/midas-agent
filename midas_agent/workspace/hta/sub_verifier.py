"""Sub-verifiers — score a hypothesis at a decision point.

A sub-verifier turns a hypothesis into a scalar score; the engine then computes
group-relative advantages from the scores. Verifiers are mostly Tier 0 (pure
code: run a probe script, parse output) so the decision mechanism is nearly
free. At most one decision-point type (spec_interpretation) may use a Tier-2
independent LLM call.

A verifier never edits production code permanently and never raises through to
the engine — the engine wraps every call and treats an exception as score 0.0.
The sandbox is reached only through the ``VerifierContext`` callables, so
verifiers are agnostic to local-vs-Docker execution.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.stdlib.react_agent import ActionRecord
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.decision_point import Hypothesis, evidence_tokens_for

logger = logging.getLogger(__name__)


@dataclass
class VerifierContext:
    """Sandbox handle passed to every sub-verifier.

    ``run_bash`` / ``write_file`` / ``remove_file`` are bound by the engine to
    real implementations (Docker IO or local subprocess). ``action_history`` is
    populated for Tier-1 verifiers that piggyback on an execution node's trace.
    """

    issue: Issue
    work_dir: str
    run_bash: Callable[[str], str]
    write_file: Callable[[str, str], str]
    remove_file: Callable[[str], None]
    action_history: list[ActionRecord] = field(default_factory=list)
    # Populated by the engine only when the verifier is dispatched in an
    # investigation_continuation context — the rule-based signal that
    # triggered IC (same_file_read_5x, budget_60pct_no_evidence,
    # same_error_3x, toolkit_repetition, max_iterations, no_action).
    # ContinuationVerifier dispatches on this; other verifiers ignore it.
    stuck_reason: str | None = None


class SubVerifier(ABC):
    """Scores one hypothesis. ``tier`` records its cost class (0/1/2)."""

    tier: int = 0

    @abstractmethod
    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        """Return a scalar score for ``hyp``. Higher is better."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

_PROBE_FILENAME = "_hta_probe.py"


def _run_probe(ctx: VerifierContext, script: str) -> str:
    """Write a probe script into the sandbox, run it, return combined output."""
    if not script.strip():
        return ""
    ctx.write_file(_PROBE_FILENAME, script)
    try:
        return ctx.run_bash(f"python {_PROBE_FILENAME} 2>&1")
    finally:
        try:
            ctx.remove_file(_PROBE_FILENAME)
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            pass


def _has_traceback(output: str) -> bool:
    return "Traceback (most recent call last)" in output or "Error" in output


# ---------------------------------------------------------------------------
# Tier-0 verifiers
# ---------------------------------------------------------------------------

class RCLVerifier(SubVerifier):
    """root_cause_localization — grep the *issue itself* for tokens that
    typically appear in tracebacks of this hypothesis class.

    The verifier does NOT run an LLM-authored probe script and does NOT
    grep an LLM-authored predicted_path string; both would let the same
    LLM call author both the question and the answer (issue #44 B3). The
    grep target is the issue's own text — which the LLM did not write —
    and the search tokens come from a static per-class lexicon in
    decision_point.RCL_EVIDENCE_TOKENS, also outside LLM control.

    Score: ``min(1.0, hits / 2)`` — 0 hits 0.0, 1 hit 0.5, 2+ hits 1.0.
    """

    tier = 0

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        tokens = evidence_tokens_for(hyp.name)
        if not tokens:
            return 0.0
        text = ctx.issue.description.lower()
        if ctx.issue.fail_to_pass:
            text += "\n" + "\n".join(ctx.issue.fail_to_pass).lower()
        hits = sum(1 for t in tokens if t.lower() in text)
        return min(1.0, hits / 2.0)


# ---------------------------------------------------------------------------
# Execution-grounded RCL probe (issue H4)
# ---------------------------------------------------------------------------

@dataclass
class RCLProbeResult:
    """One RCL hypothesis' execution-grounded probe result.

    ``method_used`` is one of (per issue H4 §3.5):

    - ``on_path``            — predicted_path is on the reproduction's
                               failing call stack (strongest signal).
    - ``exists_unconfirmed`` — predicted_path exists and the named symbol
                               appears in it, but no reproduction trace
                               was usable to confirm on-path.
    - ``lexicon_fallback``   — fell back to today's RCLVerifier text-grep
                               score (path was unverifiable AND no repro).
    - ``path_absent``        — predicted_path does not exist in the repo
                               (or the named file is missing). Scored 0.0.
    - ``skipped_budget``     — issue budget was low; probe skipped entirely
                               and fell back to text-grep.
    """

    score: float
    method_used: str
    iters_used: int = 0


class ExecutionGroundedRCLProbe:
    """Per-RCL-decision execution-grounded probe replacing RCLVerifier's
    text-grep score (issue H4 phase 1).

    The probe is constructed mechanically from machine-controlled inputs
    only (per constraint #1 — same-LLM-author-question-and-answer
    forbidden, see issue #44 B3):

    - ``hyp.predicted_path``: an LLM-proposed *path*, never a script.
    - ``ctx.issue.fail_to_pass``: the issue's failing-test list (machine-
      provided by SWE-bench, not LLM-authored).
    - The static evidence lexicon (``evidence_tokens_for(hyp.name)``).

    The reproduction trace is cached per issue (one engine instance handles
    exactly one issue, so the probe instance is short-lived and the cache
    is single-entry). All G=3 hypotheses share the same trace.

    Method selection (per §3.2, strongest available first, capped at
    ``max_iters`` sandbox commands per hypothesis):

    1. Path-existence + symbol-grounding (cheap; always available).
    2. Reproduction-trace intersection (preferred when a fail_to_pass test
       exists and reproduces).
    3. Static-lexicon fallback (today's RCLVerifier — guarantees the probe
       never scores worse than the current text-grep behaviour).
    """

    # Suggested rubric per §3.3; constants live here so unit tests can
    # import them rather than hard-coding numbers.
    SCORE_ON_PATH = 1.0
    SCORE_EXISTS_UNCONFIRMED = 0.7
    SCORE_LEXICON_FALLBACK = 0.5
    SCORE_PATH_ABSENT_SYMBOL = 0.2
    SCORE_PATH_ABSENT = 0.0

    def __init__(
        self,
        fallback_verifier: SubVerifier,
        max_iters: int = 8,
        balance_provider: Callable[[], int] | None = None,
    ) -> None:
        self._fallback = fallback_verifier
        self._max_iters = max_iters
        self._balance_provider = balance_provider
        # Per-issue cache for the reproduction traceback. Cleared when a
        # different issue is seen (defensive; in practice each engine
        # instance is per-issue).
        self._traceback_cache: str | None = None
        self._traceback_issue_id: str | None = None

    def probe(self, hyp: Hypothesis, ctx: VerifierContext) -> RCLProbeResult:
        """Score one RCL hypothesis with execution-grounded evidence.

        Phase 1b: path-existence + symbol-grounding tier only. Phase 1c
        will splice in the reproduction-trace intersection check between
        path-check and lexicon-fallback.

        Returns a :class:`RCLProbeResult` with the score, the method tier
        that produced it, and the number of sandbox commands consumed.
        """
        iters = 0

        # Budget guard: a low-budget issue gets the cheap fallback and
        # marks the result as skipped_budget so the analyzer can count
        # how often the probe was bypassed.
        if self._balance_provider is not None and self._balance_provider() <= 0:
            score = self._fallback.verify(hyp, ctx)
            return RCLProbeResult(
                score=score, method_used="skipped_budget", iters_used=0,
            )

        # Parse `predicted_path` -> (relative_file, optional symbol).
        file_rel, symbol = _parse_predicted_path(hyp.predicted_path)

        # If we have no parseable file path, the path-existence tier
        # cannot run; fall back immediately (next phase will try the
        # reproduction-trace tier here).
        if not file_rel:
            score = self._fallback.verify(hyp, ctx)
            return RCLProbeResult(
                score=score, method_used="lexicon_fallback", iters_used=iters,
            )

        # Tier 1: path-existence check. One sandbox command.
        path_exists = _check_path_exists(ctx, file_rel)
        iters += 1
        if not path_exists:
            return RCLProbeResult(
                score=self.SCORE_PATH_ABSENT,
                method_used="path_absent",
                iters_used=iters,
            )

        # Tier 2: symbol grounding (only if a symbol was named AND iters
        # budget permits — leave headroom for phase 1c's reproduction
        # check, so cap symbol-grep at one additional call).
        symbol_present = False
        if symbol and iters < self._max_iters:
            symbol_present = _check_symbol_in_file(ctx, file_rel, symbol)
            iters += 1

        # Score floor when path exists but symbol can't be confirmed.
        if symbol and not symbol_present:
            return RCLProbeResult(
                score=self.SCORE_PATH_ABSENT_SYMBOL,
                method_used="path_absent",  # symbol absent ≈ path didn't ground
                iters_used=iters,
            )

        # Path exists (and symbol — if named — present). Phase 1c will
        # promote this to ``on_path`` if a reproduction confirms the path
        # is on the failing call stack. For now, return the
        # exists_unconfirmed score.
        return RCLProbeResult(
            score=self.SCORE_EXISTS_UNCONFIRMED,
            method_used="exists_unconfirmed",
            iters_used=iters,
        )


# ---------------------------------------------------------------------------
# Probe helpers — pure functions, easy to unit-test
# ---------------------------------------------------------------------------

def _parse_predicted_path(raw: str) -> tuple[str | None, str | None]:
    """Parse an LLM-proposed ``predicted_path`` into a (file, symbol) pair.

    The LLM frequently writes one of:
      ``astropy/table/table.py``
      ``astropy/table/table.py:_check_required_columns``
      ``astropy/table/table.py:Table.__init__``
    Occasionally it appends free-form prose (mixed-language paths from
    earlier runs):
      ``astropy/table/table.py中设置列数据的代码路径(可能在Table.__init__...)``

    The file is the longest prefix that looks like ``<some>/<path>.py``;
    the symbol (if any) is the first colon-separated token after the
    file. Returns ``(None, None)`` for unparseable input.
    """
    if not raw or not isinstance(raw, str):
        return None, None
    cleaned = raw.strip()
    # Find the first .py occurrence; anything before its closing extension
    # boundary is the file path. We allow a leading "/" to preserve
    # absolute paths so _sandbox_path doesn't double-prefix.
    import re as _re
    m = _re.search(r"(/?[A-Za-z0-9_./\-]+\.py)", cleaned)
    if not m:
        return None, None
    file_path = m.group(1)
    rest = cleaned[m.end():].lstrip()
    symbol = None
    # Strip a leading ':' or '#' and take the next identifier-shaped run.
    if rest.startswith((":", "#")):
        rest = rest[1:].lstrip()
    sym_match = _re.match(r"[A-Za-z_][A-Za-z0-9_.]*", rest)
    if sym_match:
        symbol = sym_match.group(0)
    return file_path, symbol


def _sandbox_path(ctx: VerifierContext, rel: str) -> str:
    """Absolute path inside the sandbox for a relative repo path."""
    # In Docker mode ctx.work_dir is typically "/testbed"; in local mode
    # it is the agent's working dir. Either way, prefixing yields the
    # path the run_bash command will see.
    import os as _os
    if rel.startswith("/"):
        return rel
    return _os.path.join(ctx.work_dir or "/testbed", rel)


def _check_path_exists(ctx: VerifierContext, rel: str) -> bool:
    """Return True iff ``<work_dir>/<rel>`` exists as a regular file."""
    abs_path = _sandbox_path(ctx, rel)
    # Quoting is enough — predicted_path comes from the LLM but we have
    # already restricted it to chars matching ``[A-Za-z0-9_./-]`` in
    # _parse_predicted_path, so command injection is not a real risk.
    out = ctx.run_bash(f"test -f {abs_path!s} && echo HTA_PROBE_EXISTS || echo HTA_PROBE_MISSING") or ""
    return "HTA_PROBE_EXISTS" in out


def _check_symbol_in_file(ctx: VerifierContext, rel: str, symbol: str) -> bool:
    """Return True iff ``symbol`` appears in ``<work_dir>/<rel>``.

    Uses fixed-string grep — no regex metacharacters in user input get
    interpreted. Symbols are constrained to ``[A-Za-z_][A-Za-z0-9_.]*``
    by the parser so they are shell-safe.
    """
    abs_path = _sandbox_path(ctx, rel)
    out = ctx.run_bash(f"grep -F -q -- {symbol!r} {abs_path!s} && echo HTA_SYMBOL_FOUND || echo HTA_SYMBOL_ABSENT") or ""
    return "HTA_SYMBOL_FOUND" in out


_LAYER_HIT_SENTINEL = "HTA_LAYER_HIT"


class FixLocalityVerifier(SubVerifier):
    """fix_locality_scope — sentinel-assert layer probe.

    The probe (LLM-authored) inserts an assertion at the candidate layer
    tagged with the exact message ``HTA_LAYER_HIT`` and runs the issue's
    reproduction. The verifier scores by whether that sentinel-tagged
    assertion fires during reproduction — i.e. whether the candidate
    layer is on the failing code path.

    The previous implementation returned 1.0 in three of five branches
    (any non-error output → confirmed), which gave no discrimination at
    all; every hypothesis tied at 0.5/1.0 and the winner defaulted to
    the first class in the seed list (issue #44 B4).

    Scoring:
      1.0  sentinel-tagged assertion fired (named layer is on the path)
      0.3  some other AssertionError fired (wrong layer)
      0.5  unrelated traceback (probe ran but inconclusive)
      0.2  probe ran cleanly without firing the sentinel (layer not reached)
      0.0  no probe payload

    Anti-gaming: hypotheses compete in groups of G; gaming one (e.g.
    ``assert True, 'HTA_LAYER_HIT'`` everywhere) requires gaming all of
    them, which produces a tie and collapses std → 0 → no winner
    advantage. The competitive dynamic enforces honest probes.
    """

    tier = 1

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        if not hyp.test_payload.strip():
            return 0.0
        output = _run_probe(ctx, hyp.test_payload)
        if f"AssertionError: {_LAYER_HIT_SENTINEL}" in output or _LAYER_HIT_SENTINEL in output:
            return 1.0
        if "AssertionError" in output:
            return 0.3
        if _has_traceback(output):
            return 0.5
        return 0.2


_JUDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_judgment",
        "description": (
            "Score how well the given hypothesis explains the issue. "
            "Return a single float in [0.0, 1.0]."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "score": {
                    "type": "number",
                    "description": "0.0 = does not fit at all, 1.0 = strongly fits.",
                },
                "reason": {"type": "string", "description": "One short sentence."},
            },
            "required": ["score"],
        },
    },
}

_JUDGE_PROMPT = """\
You are scoring how well one hypothesis explains a GitHub issue.

## Issue
{issue}

## Hypothesis
class: {hypothesis_class}
rationale: {rationale}

Score in [0.0, 1.0]: 0.0 means the hypothesis does not fit the issue at all,
1.0 means it is a strong, specific fit. Be conservative — most hypotheses on
unfamiliar issues should score 0.3-0.6. Reserve 0.8+ for genuinely strong
matches. Call submit_judgment exactly once with your score and one-sentence
reason.\
"""


class LLMJudgeVerifier(SubVerifier):
    """Tier-2 generic verifier — one LLM call scores a hypothesis against the issue.

    Used (a) by SpecInterpretationVerifier when the cheap lexical score is
    inconclusive, and (b) by HTAEngine to force Tier-2 verification on
    novel-class hypotheses (courseware §003 novel-class exception). The
    engine is responsible for the per-issue 3-call cap by gating who calls
    ``verify``; this class itself does not count.
    """

    tier = 2

    def __init__(self, system_llm: Callable[[LLMRequest], LLMResponse]) -> None:
        self._system_llm = system_llm

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        prompt = _JUDGE_PROMPT.format(
            issue=(ctx.issue.description or "").strip()[:2000] or "(no description)",
            hypothesis_class=hyp.name,
            rationale=(hyp.rationale or "").strip()[:500] or "(no rationale)",
        )
        messages = [
            {"role": "system", "content": "You judge hypothesis-issue fit. Always call submit_judgment."},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self._system_llm(
                LLMRequest(messages=messages, model="default", tools=[_JUDGE_TOOL]),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("LLMJudgeVerifier API error: %s", e)
            return 0.0
        if not resp.tool_calls:
            return 0.0
        for tc in resp.tool_calls:
            if tc.name != "submit_judgment":
                continue
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return 0.0
            if not isinstance(args, dict):
                return 0.0
            try:
                score = float(args.get("score", 0.0))
            except (TypeError, ValueError):
                return 0.0
            return max(0.0, min(1.0, score))
        return 0.0


class SpecInterpretationVerifier(SubVerifier):
    """spec_interpretation — score a reading by lexical consistency with the
    issue text, escalating to Tier-2 LLM judgment when lexical is inconclusive.

    Per courseware §003 + §008, this is the one decision point whose default
    verifier may use a Tier-2 LLM call. The engine still owns the per-issue
    cap on Tier-2 calls; this class will skip the LLM call when no
    ``system_llm`` is wired.
    """

    tier = 0

    def __init__(self, system_llm: Callable[[LLMRequest], LLMResponse] | None = None) -> None:
        self._system_llm = system_llm
        if system_llm is not None:
            self.tier = 2
            self._judge = LLMJudgeVerifier(system_llm)
        else:
            self._judge = None

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        lexical = self._lexical_overlap(hyp, ctx)
        # Decisive in either direction — accept the cheap score.
        if lexical < 0.1 or lexical > 0.7:
            return lexical
        # Lexical inconclusive — escalate to the LLM judge if available.
        if self._judge is None:
            return lexical
        return self._judge.verify(hyp, ctx)

    @staticmethod
    def _lexical_overlap(hyp: Hypothesis, ctx: VerifierContext) -> float:
        text = (ctx.issue.description + " " + " ".join(ctx.issue.fail_to_pass)).lower()
        rationale_terms = {
            t for t in (hyp.rationale + " " + hyp.predicted_path).lower().split()
            if len(t) > 3
        }
        if not rationale_terms:
            return 0.0
        overlap = sum(1 for t in rationale_terms if t in text)
        return overlap / len(rationale_terms)


class ContinuationVerifier(SubVerifier):
    """investigation_continuation — Tier-1 heuristic that reads the stuck
    signal that triggered IC, not just adjacent action repetition.

    The verifier's job: given the *reason* the agent got stuck, score each
    continuation strategy by how appropriate it is for that reason. Across
    the 30-issue eval the legacy adjacent-repetition formula collapsed IC's
    verdict space to 100% ``persist_same_path``; reading stuck_reason
    restores the verdict diversity the rescue mechanism is supposed to
    produce.

    The legacy behaviour (count adjacent action repeats) is preserved as
    the fallback when ``stuck_reason`` is None or unrecognised, so unknown
    future signals don't silently break IC.
    """

    tier = 1

    # Per-class baselines for each known stuck_reason. The verifier returns
    # baseline + a small history-derived adjustment. Designed so that the
    # right verdict beats the wrong ones by ~0.3 — enough for advantage to
    # be decisive, std stays well above epsilon.
    _SCORES_BY_REASON: dict[str, dict[str, float]] = {
        # Same file read 5+ times: agent is loop-stuck on one file. The
        # right move is to look elsewhere — pivot_target wins, abandon is
        # second, persist is penalised.
        "same_file_read_5x": {
            "persist_same_path": 0.25,
            "pivot_target": 0.80,
            "pivot_evidence_type": 0.55,
            "abandon": 0.55,
        },
        # >=60% budget burned without predicted evidence: hypothesis is
        # likely wrong; abandon and re-enter parent decision.
        "budget_60pct_no_evidence": {
            "persist_same_path": 0.15,
            "pivot_target": 0.50,
            "pivot_evidence_type": 0.50,
            "abandon": 0.85,
        },
        # Same error repeating: agent keeps producing the same failure
        # signal. The right move is to pivot the evidence type (look at
        # different signals), not stop trying.
        "same_error_3x": {
            "persist_same_path": 0.25,
            "pivot_target": 0.55,
            "pivot_evidence_type": 0.80,
            "abandon": 0.50,
        },
        # Toolkit's verbatim-repetition detector fired: soft signal, could
        # be a legitimate retry. Keep persist allowed but lowered.
        "toolkit_repetition": {
            "persist_same_path": 0.55,
            "pivot_target": 0.55,
            "pivot_evidence_type": 0.55,
            "abandon": 0.30,
        },
        # Terminal-style stuck (no more iterations / no action emitted):
        # the agent has effectively given up. Abandon is the honest call.
        "max_iterations": {
            "persist_same_path": 0.30,
            "pivot_target": 0.50,
            "pivot_evidence_type": 0.45,
            "abandon": 0.75,
        },
        "no_action": {
            "persist_same_path": 0.25,
            "pivot_target": 0.45,
            "pivot_evidence_type": 0.40,
            "abandon": 0.75,
        },
    }

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        reason = ctx.stuck_reason
        # Map reasons that include a payload suffix (e.g.
        # "same_file_read_5x:foo.py") to their bucket name.
        bucket = reason.split(":", 1)[0] if reason else None

        if bucket and bucket in self._SCORES_BY_REASON:
            scores = self._SCORES_BY_REASON[bucket]
            base = scores.get(hyp.name, 0.30)
            # Small adjustment from action history: a longer trace mildly
            # boosts abandon, mildly suppresses persist. Capped so it
            # cannot overturn the baseline.
            n = len(ctx.action_history)
            length_bonus = 0.1 if n > 20 else 0.0
            if hyp.name == "abandon":
                return min(1.0, base + length_bonus)
            if hyp.name == "persist_same_path":
                return max(0.0, base - length_bonus)
            return base

        # Fallback: stuck_reason is None or unrecognised. Use the legacy
        # adjacent-repetition formula so unknown future signals don't
        # silently break IC.
        return self._legacy_score(hyp, ctx)

    @staticmethod
    def _legacy_score(hyp: Hypothesis, ctx: VerifierContext) -> float:
        history = ctx.action_history
        n = len(history)
        repeats = sum(
            1 for a, b in zip(history, history[1:])
            if a.action_name == b.action_name and a.arguments == b.arguments
        )
        repetition = repeats / n if n else 0.0
        cls = hyp.name
        if cls == "abandon":
            return min(1.0, 0.3 + repetition + (0.3 if n > 20 else 0.0))
        if cls == "persist_same_path":
            return max(0.0, 1.0 - repetition - (0.3 if n > 20 else 0.0))
        if cls in ("pivot_evidence_type", "pivot_target"):
            return 0.4 + 0.4 * repetition
        return 0.3


class TestScopeVerifier(SubVerifier):
    """test_scope_strategy (optional DP) — run the hypothesis' test command and
    score by how cleanly it passes. Broader scopes that still pass score higher.
    """

    tier = 0

    _SCOPE_WEIGHT = {
        "targeted_only": 0.7,
        "module_local": 0.85,
        "cross_module": 0.95,
        "full_repo_sample": 1.0,
    }

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        if not hyp.test_payload.strip():
            return 0.0
        output = ctx.run_bash(hyp.test_payload)
        weight = self._SCOPE_WEIGHT.get(hyp.name, 0.6)
        if _has_traceback(output) or "failed" in output.lower():
            return 0.2 * weight
        return weight
