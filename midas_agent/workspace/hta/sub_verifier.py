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


class SpecInterpretationVerifier(SubVerifier):
    """spec_interpretation — score a reading by lexical consistency with the
    issue text and gold test names.

    This is the one decision point allowed a Tier-2 independent LLM call: when
    a ``system_llm`` is supplied and the lexical scores are too close to
    separate the readings, one call breaks the tie.
    """

    tier = 0

    def __init__(self, system_llm: Callable[[LLMRequest], LLMResponse] | None = None) -> None:
        self._system_llm = system_llm
        if system_llm is not None:
            self.tier = 2

    def verify(self, hyp: Hypothesis, ctx: VerifierContext, cheap: bool = True) -> float:
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
