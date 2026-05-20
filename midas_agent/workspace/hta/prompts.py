"""HTA prompt templates and structured-output tool schemas.

All LLM-facing text for the decision layer lives here: hypothesis generation
and the decision-point meta-judge. Execution nodes reuse the repo-wide
SYSTEM_PROMPT from midas_agent.prompts.
"""

# ---------------------------------------------------------------------------
# Hypothesis generation
# ---------------------------------------------------------------------------

SUBMIT_HYPOTHESES_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_hypotheses",
        "description": (
            "Submit exactly G mutually exclusive hypotheses for this decision "
            "point. Call this once after reasoning."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "hypothesis_class": {
                                "type": "string",
                                "description": (
                                    "One of the listed seed classes, or "
                                    "'__novel__:<short-slug>' if none fit."
                                ),
                            },
                            "rationale": {
                                "type": "string",
                                "description": "Why this hypothesis is plausible (1-3 sentences).",
                            },
                            "predicted_path": {
                                "type": "string",
                                "description": (
                                    "The file/function the evidence should implicate "
                                    "(localization), or the code layer to patch "
                                    "(fix locality). Be concrete."
                                ),
                            },
                            "test_payload": {
                                "type": "string",
                                "description": (
                                    "For root_cause_localization: optional — the "
                                    "verifier no longer runs probe scripts at RCL "
                                    "(it greps the issue itself), so this field is "
                                    "diagnostic-only here. "
                                    "For fix_locality_scope: a short script that "
                                    "(a) inserts an assertion at your candidate layer "
                                    "tagged with the EXACT message 'HTA_LAYER_HIT' "
                                    "(e.g. `assert <condition>, 'HTA_LAYER_HIT'`) and "
                                    "(b) runs the issue's reproduction. The verifier "
                                    "scores by whether that sentinel-tagged assertion "
                                    "fires; an honest probe with a discriminating "
                                    "condition will only fire on the right layer. "
                                    "May be empty for other decision types."
                                ),
                            },
                        },
                        "required": ["hypothesis_class", "rationale", "predicted_path"],
                    },
                },
            },
            "required": ["hypotheses"],
        },
    },
}

HYPOTHESIS_GEN_PROMPT = """\
You are generating hypotheses at a decision point for a coding agent solving a \
GitHub issue.

## Decision point
type: {decision_type}
{decision_type_help}

## Seed hypothesis classes
{seed_classes}

If none of the seed classes fit, you MAY name a new one as \
'__novel__:<short-slug>' — but prefer a seed class when one is reasonable.

## Issue
{issue_description}

## Evidence gathered so far
{evidence}

## Historical priors
{bias_summary}

## Your task
Produce EXACTLY {g} hypothes{es_suffix}. They MUST be mutually exclusive — no \
two may be true at once. {g_guidance}

Prefer high-advantage classes from the historical priors, but you are not bound \
by them: propose the hypotheses the evidence actually supports. Then call the \
submit_hypotheses tool.\
"""

G1_GUIDANCE = (
    "History strongly favours one class for this decision type — commit to the "
    "single most plausible hypothesis and make its predicted_path and "
    "test_payload as concrete as possible."
)
GN_GUIDANCE = (
    "Make the hypotheses genuinely different root explanations, not variations "
    "of one idea."
)

DECISION_TYPE_HELP = {
    "root_cause_localization": (
        "Where is the bug's root cause? Each hypothesis names a structural cause "
        "and a concrete predicted path, plus a <=10-line reproduction script."
    ),
    "fix_locality_scope": (
        "At which layer should the fix be applied? Each hypothesis names a layer "
        "and the assert snippet that would prove that layer is the right one."
    ),
    "spec_interpretation": (
        "How should the issue's specification be read? Each hypothesis is a "
        "distinct reading of the requirement."
    ),
    "investigation_continuation": (
        "The current investigation is stuck. Each hypothesis is a distinct way "
        "to proceed: persist, pivot the evidence type, pivot the target, or abandon."
    ),
    "test_scope_strategy": (
        "Which tests should validate the fix? Each hypothesis is a distinct test "
        "scope."
    ),
}

# ---------------------------------------------------------------------------
# Decision-point meta-judge
# ---------------------------------------------------------------------------

JUDGE_DECISION_POINT_TOOL = {
    "type": "function",
    "function": {
        "name": "judge_decision_point",
        "description": "Report whether the agent's current state is a real decision point.",
        "parameters": {
            "type": "object",
            "properties": {
                "is_decision_point": {"type": "boolean"},
                "decision_type": {
                    "type": "string",
                    "description": (
                        "'investigation_continuation', or '__novel__:<slug>' for a "
                        "newly identified decision-point type."
                    ),
                },
                "path_dependency": {
                    "type": "boolean",
                    "description": "Does choosing wrong make large later work meaningless?",
                },
                "enumerable_alternatives": {
                    "type": "boolean",
                    "description": "Can 2-K mutually exclusive choices be named right now?",
                },
                "delayed_verification": {
                    "type": "boolean",
                    "description": "Is there NO cheap (<5s) oracle that reveals the answer?",
                },
                "reason": {"type": "string"},
            },
            "required": [
                "is_decision_point",
                "path_dependency",
                "enumerable_alternatives",
                "delayed_verification",
                "reason",
            ],
        },
    },
}

META_JUDGE_PROMPT = """\
You are evaluating whether a coding agent has reached a decision point worth \
opening parallel hypotheses. You see only its recent trace — no other context.

Be conservative: most agent states are NOT decision points. Default to "no".

## Issue summary
{issue_summary}

## Recent agent trace
{trace}

## Three conditions — ALL must hold for a real decision point
1. Path dependency: choosing wrong makes substantial later work meaningful only \
under that choice; reversal is expensive.
2. Enumerable alternatives: 2-K mutually exclusive choices can be named right \
now, not in hindsight.
3. Delayed verification: no cheap (<5s) oracle would immediately reveal the \
correct choice.

## States that are NOT decision points (the default)
- Routine information gathering or following an established lead.
- Cosmetic or trivially reversible changes.
- The agent is making steady progress.

Call judge_decision_point with your verdict. Set is_decision_point=true ONLY if \
all three conditions hold.\
"""
