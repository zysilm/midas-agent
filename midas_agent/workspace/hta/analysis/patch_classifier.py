"""Classify a patch's action type for HTA evaluation (issue #46 Fix 2).

Heuristic. Partitions patches into 7 buckets so the 30-issue
distribution can be cross-tabbed by (RCL-outcome × action-type) in the
post-run report's Table 4. Not publication-grade; the goal is "good
enough that cell counts are meaningful", not perfect classification.

The seven buckets, with their action_family() coarse-graining:
  remove_behavior  -> remove
  add_warning      -> add
  add_guard        -> add
  add_branch       -> add
  modify_message   -> modify
  modify_logic     -> modify
  mixed            -> modify
"""
from __future__ import annotations

import re

WARN_TOKENS = (
    "warnings.warn",
    "DeprecationWarning",
    "FutureWarning",
    "PendingDeprecationWarning",
)
# Match new control-flow openers (raise/if/elif/assert) introduced in additions.
NEW_BRANCH_RE = re.compile(r"^\+\s*(if|elif|raise|assert)\b")
NEW_FUNC_RE = re.compile(r"^\+\s*def\s+\w+\s*\(")
# A pure string-literal addition (e.g. a single error-message line).
STRING_LIT_RE = re.compile(r'^\+\s*["\'].*["\']\s*,?\s*$')

CATEGORIES = (
    "remove_behavior", "add_warning", "add_guard", "add_branch",
    "modify_message", "modify_logic", "mixed",
)


def _split_lines(patch_text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (added_content_lines, removed_content_lines, touched_files)."""
    added = [
        l for l in patch_text.splitlines()
        if l.startswith("+") and not l.startswith("+++")
    ]
    removed = [
        l for l in patch_text.splitlines()
        if l.startswith("-") and not l.startswith("---")
    ]
    files = re.findall(r"^diff --git a/(\S+)", patch_text, re.MULTILINE)
    return added, removed, files


def _non_blank(lines: list[str]) -> list[str]:
    """Drop blank '+' / '-' lines (markers with no real content)."""
    return [l for l in lines if l.lstrip("+-").strip()]


def classify(patch_text: str) -> str:
    if not patch_text or not patch_text.strip():
        return "mixed"
    added, removed, files = _split_lines(patch_text)
    nb_added = _non_blank(added)
    nb_removed = _non_blank(removed)

    # 1. add_warning — any of the deprecation tokens appearing in additions.
    if any(any(t in l for t in WARN_TOKENS) for l in added):
        return "add_warning"

    # 2. remove_behavior — material net deletion. Counts on non-blank lines so
    #    whitespace noise doesn't dominate. The thresholds (>=3 removals AND
    #    removals strictly more than additions, with very few additions)
    #    capture the canonical "5 lines deleted, 0 lines added" smoke v1 ep3
    #    case as well as removals with a one-line stub.
    if len(nb_removed) >= 3 and len(nb_removed) > len(nb_added) + 1:
        return "remove_behavior"

    # 3. add_branch — a brand-new function/method definition was inserted.
    if any(NEW_FUNC_RE.match(l) for l in added):
        return "add_branch"

    # 4. add_guard — at least two new control-flow openers
    #    (if/elif/raise/assert), at least as many as removals.
    new_branches = sum(1 for l in nb_added if NEW_BRANCH_RE.match(l))
    if new_branches >= 2 and new_branches >= len(nb_removed):
        return "add_guard"

    # 5. modify_message — small (≤4 line) additions that are all string
    #    literals (typical error-message tweak).
    if 1 <= len(nb_added) <= 4 and all(STRING_LIT_RE.match(l) for l in nb_added):
        return "modify_message"

    # 6. modify_logic — same-shape edit (similar add/remove count) confined
    #    to a single file. Captures one-line refactors and the like.
    if len(files) == 1 and abs(len(nb_added) - len(nb_removed)) <= 2:
        return "modify_logic"

    return "mixed"


def action_family(category: str) -> str:
    """Coarse-grain the 7 categories to 3 families for the 4-quadrant table."""
    if category == "remove_behavior":
        return "remove"
    if category in ("add_warning", "add_guard", "add_branch"):
        return "add"
    return "modify"
