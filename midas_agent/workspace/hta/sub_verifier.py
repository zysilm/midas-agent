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
    """investigation_continuation — Tier-1 heuristic over the stuck node's trace.

    Scores each continuation strategy from observable signals in the action
    history rather than spending a fresh LLM call.
    """

    tier = 1

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
        history = ctx.action_history
        n = len(history)
        # Repetition ratio: how many of the last actions repeat the prior one.
        repeats = sum(
            1 for a, b in zip(history, history[1:])
            if a.action_name == b.action_name and a.arguments == b.arguments
        )
        repetition = repeats / n if n else 0.0

        cls = hyp.name
        if cls == "abandon":
            # Abandon looks better the more repetitive / longer the dead end is.
            return min(1.0, 0.3 + repetition + (0.3 if n > 20 else 0.0))
        if cls == "persist_same_path":
            # Persisting looks better when the trace is short and not repetitive.
            return max(0.0, 1.0 - repetition - (0.3 if n > 20 else 0.0))
        if cls in ("pivot_evidence_type", "pivot_target"):
            # Pivoting is a middle option — moderately favoured under repetition.
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
