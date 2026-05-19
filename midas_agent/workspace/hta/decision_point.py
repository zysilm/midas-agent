"""Decision points and hypotheses — HTA's structured-decision layer.

A *decision point* is a step that satisfies all three criteria: path
dependency, enumerable mutually-exclusive alternatives, and delayed
verification. The default presumption is that a step is NOT a decision point.

This module defines the data model (Hypothesis, DecisionPoint) and the
DecisionPointRegistry holding the seed decision points. Sub-verifiers are NOT
attached here — the engine owns a ``decision_type -> SubVerifier`` map (built
in sub_verifier.py); a DecisionPoint only records which verifier *tier* it uses.
"""
from __future__ import annotations

from dataclasses import dataclass

# The slug used for a hypothesis class that does not fit any seed class.
NOVEL_PREFIX = "__novel__"


@dataclass
class Hypothesis:
    """One falsifiable, mutually-exclusive commitment at a decision point."""

    name: str                        # hypothesis_class, or "__novel__:<slug>"
    rationale: str = ""
    predicted_path: str = ""         # file/function the evidence should implicate
    test_payload: str = ""           # repro script / assert snippet
    distilled_evidence: str = ""     # set after the winner is chosen
    score: float = 0.0               # raw sub_verifier score
    advantage: float = 0.0           # group-relative advantage

    @property
    def is_novel(self) -> bool:
        return self.name.startswith(NOVEL_PREFIX)

    @property
    def novel_slug(self) -> str | None:
        """The slug part of a "__novel__:<slug>" name, else None."""
        if not self.is_novel:
            return None
        _, _, slug = self.name.partition(":")
        return slug or "unspecified"


@dataclass
class DecisionPoint:
    """Static specification of one decision-point type."""

    decision_type: str
    seed_classes: list[str]
    trigger_kind: str = "rule"       # "rule" | "meta_judge"
    verifier_tier: int = 0           # 0 pure-code | 1 piggyback | 2 independent LLM
    allow_tier2: bool = False        # may the sub_verifier make an LLM call?
    budget_hint: int = 0             # advisory only — budget is a global safety brake


@dataclass
class RuleTriggerInputs:
    """Boolean state flags the engine feeds to rule-based DP triggering."""

    is_issue_start: bool = False
    rcl_winner_found: bool = False
    about_to_modify_code: bool = False
    rcl_all_negative: bool = False
    test_scope_pending: bool = False


# Seed hypothesis-class lists -------------------------------------------------

RCL_CLASSES = [
    "framework_default_value",
    "operator_overload_path",
    "serialization_roundtrip",
    "inheritance_dispatch",
    "regex_or_parser_edge",
    "state_mutation_order",
    "error_message_only",
    "test_expectation_wrong",
]
FIX_LOCALITY_CLASSES = ["surface_patch", "intermediate_layer", "root_layer", "dual_fix"]
SPEC_INTERPRETATION_CLASSES = [
    "literal_reading",
    "inverse_reading",
    "scope_widened",
    "scope_narrowed",
    "wrong_api",
]
CONTINUATION_CLASSES = ["persist_same_path", "pivot_evidence_type", "pivot_target", "abandon"]
TEST_SCOPE_CLASSES = ["targeted_only", "module_local", "cross_module", "full_repo_sample"]


class DecisionPointRegistry:
    """Holds the seed decision points.

    The four core DPs are always present. ``test_scope_strategy`` is an optional
    5th DP — gated by ``enable_test_scope`` — because the shipped architecture
    treats test runs as plain execution nodes; it stays registered-but-off so a
    future config can enable it without code changes.
    """

    def __init__(self, enable_test_scope: bool = False) -> None:
        self._dps: dict[str, DecisionPoint] = {}
        self._enable_test_scope = enable_test_scope

        self._register(DecisionPoint(
            decision_type="root_cause_localization",
            seed_classes=list(RCL_CLASSES),
            trigger_kind="rule",
            verifier_tier=0,
        ))
        self._register(DecisionPoint(
            decision_type="fix_locality_scope",
            seed_classes=list(FIX_LOCALITY_CLASSES),
            trigger_kind="rule",
            verifier_tier=0,
        ))
        self._register(DecisionPoint(
            decision_type="spec_interpretation",
            seed_classes=list(SPEC_INTERPRETATION_CLASSES),
            trigger_kind="rule",
            verifier_tier=0,
            allow_tier2=True,   # the one DP allowed an independent LLM call
        ))
        self._register(DecisionPoint(
            decision_type="investigation_continuation",
            seed_classes=list(CONTINUATION_CLASSES),
            trigger_kind="meta_judge",
            verifier_tier=1,
        ))
        if enable_test_scope:
            self._register(DecisionPoint(
                decision_type="test_scope_strategy",
                seed_classes=list(TEST_SCOPE_CLASSES),
                trigger_kind="rule",
                verifier_tier=0,
            ))

    def _register(self, dp: DecisionPoint) -> None:
        self._dps[dp.decision_type] = dp

    def get(self, decision_type: str) -> DecisionPoint | None:
        return self._dps.get(decision_type)

    def all(self) -> list[DecisionPoint]:
        return list(self._dps.values())

    def decision_types(self) -> list[str]:
        return list(self._dps)

    @property
    def test_scope_enabled(self) -> bool:
        return self._enable_test_scope

    def rule_triggered(self, inputs: RuleTriggerInputs) -> DecisionPoint | None:
        """Return the rule-triggered decision point for the current state, if any.

        Priority order matches the architecture's main flow: RCL fires once at
        issue start; spec_interpretation pre-empts when RCL's cheap checks all
        came back negative; fix_locality_scope fires before the first code edit.
        """
        if inputs.is_issue_start:
            return self._dps.get("root_cause_localization")
        if inputs.rcl_all_negative:
            return self._dps.get("spec_interpretation")
        if inputs.rcl_winner_found and inputs.about_to_modify_code:
            return self._dps.get("fix_locality_scope")
        if self._enable_test_scope and inputs.test_scope_pending:
            return self._dps.get("test_scope_strategy")
        return None
