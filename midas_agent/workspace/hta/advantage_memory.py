"""SemanticExperienceMemory — append-only log of distilled HTA decision lessons.

Replaces the numerical ``TypedAdvantageMemory`` (issue H3, 2026-05-21). The
old class accumulated per-``(decision_type, hypothesis_class)`` running
statistics (mean / variance, asymmetric EMA), but those numbers were a
training-time policy-gradient concept used at inference time: they had no
causal path to behaviour. ``argmax A_i`` inside a group is monotonic with
``argmax score_i`` so the rescaling was a no-op, and the cross-issue prompt
injection of ``mean = +0.42`` gave the LLM an arbitrary number it had no way
to act on.

This module records concrete past experiences instead: one
:class:`SemanticMemoryEntry` per decision point, holding a winner_summary,
a counterfactual_summary, and the episode outcome. Future hypothesis-gen
prompts at the same decision type retrieve the top-K relevant entries via
``bias_summary`` and inject them as narrative guidance — *what worked* and
*what didn't*, not aggregated statistics.

Methodologically aligned with *Training-Free Group Relative Policy
Optimization* (arXiv:2510.08191, Oct 2025): they reach the same conclusion
for math reasoning; we instantiate the same idea for SWE-bench decision
points.

Persistence: atomic JSON via ``.tmp`` + ``os.replace``, loaded on
construction. Schema version 2. v1 numerical stores are not migrated —
fresh ``train_dir`` per evaluation protocol means cold starts are clean.
The per-episode ``_pending`` buffer is never persisted; it is applied
(and cleared) by :meth:`commit_pending` at ``post_episode``, so a crashed
episode cannot poison the memory.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SemanticMemoryEntry:
    """One distilled lesson from one decision point in one episode.

    Append-only. The replacement for the old ``AdvantageStat`` — instead of
    "class X has mean advantage +0.42 over n=8 observations", we record
    "at decision point X in issue Y, the agent chose class Z because W, and
    classes A and B were less plausible because V, and the episode outcome
    was 0.8". The LLM reads concrete past experiences at the next decision
    point; it does not read aggregated statistics.
    """

    decision_type: str          # e.g. "root_cause_localization"
    winner_class: str           # the hypothesis_class that won (seed or __novel__:slug)
    winner_summary: str         # 1-2 sentences: why this won
    counterfactual_summary: str # 1-2 sentences: why the losers lost
    outcome_score: float        # episode s_exec, in [0.0, 1.0]
    issue_id: str               # e.g. "astropy__astropy-12907"
    timestamp: float            # epoch seconds at entry creation
    is_novel_winner: bool       # winner_class.startswith("__novel__")


# Cold-start placeholder shown when no prior experience exists for a
# given decision type. Phrased so the LLM recognises it as "no priors,
# don't try to invent a class to match anything".
_COLD_START_PLACEHOLDER = (
    "No prior experience for this decision type — make a fresh decision "
    "based on the issue and evidence."
)


class SemanticExperienceMemory:
    """Append-only log of semantic memory entries, with simple structured
    retrieval at decision-point time. Replaces ``TypedAdvantageMemory``.

    Shared across all workspaces and episodes in a run — constructed once
    by the workspace manager and handed to every HTAWorkspace, which is
    what makes the cold-to-warm transition meaningful.

    Persistence schema (v2)::

        {
          "schema_version": 2,
          "entries": [ {<SemanticMemoryEntry as dict>}, ... ],
          "novel_counter": { <slug>: <count>, ... },
          "registered_novel": [<slug>, ...]
        }

    Cold start = empty entries list. v1 numerical stores are not migrated.
    """

    SCHEMA_VERSION = 2
    MAX_BIAS_SUMMARY_TOKENS = 800   # cap on prompt-injected experience
    DEFAULT_K = 5                   # entries retrieved per decision type

    def __init__(
        self,
        store_path: str,
        novel_register_threshold: int = 3,
    ) -> None:
        self._store_path = store_path
        self._novel_register_threshold = novel_register_threshold

        self._entries: list[SemanticMemoryEntry] = []
        # Buffered within an episode; applied at post_episode.
        self._pending: list[SemanticMemoryEntry] = []
        # Slug -> times seen, for __novel__ auto-registration.
        self._novel_counter: dict[str, int] = {}
        self._registered_novel: set[str] = set()

        if os.path.isfile(store_path):
            self.load()

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def buffer(self, entry: SemanticMemoryEntry) -> None:
        """Buffer one pending entry. Called by the engine after distillation
        produces it. The entry is committed to the persistent log by
        :meth:`commit_pending` at ``post_episode``.
        """
        if not isinstance(entry, SemanticMemoryEntry):
            raise TypeError(
                "SemanticExperienceMemory.buffer expects a SemanticMemoryEntry "
                f"(got {type(entry).__name__})"
            )
        self._pending.append(entry)

    def commit_pending(self, outcome_score: float) -> None:
        """Stamp each pending entry with the episode outcome and append to the log.

        ``outcome_score`` is the episode's execution score (s_exec, in [0, 1]).
        Failed-episode entries are stamped as failures so ``bias_summary`` can
        surface them as "what didn't work" guidance. Called once from
        :meth:`HTAWorkspace.post_episode`.
        """
        clamped = max(0.0, min(1.0, float(outcome_score)))
        for entry in self._pending:
            entry.outcome_score = clamped
            self._entries.append(entry)
        n = len(self._pending)
        self._pending.clear()
        if n:
            self.save()
            logger.info(
                "SemanticExperienceMemory: committed %d entries (outcome=%.2f)",
                n, clamped,
            )

    def discard_pending(self) -> None:
        """Drop buffered entries without applying them (e.g. crashed episode)."""
        self._pending.clear()

    # ------------------------------------------------------------------
    # Read path (phase C will fill in retrieval ranking + formatting)
    # ------------------------------------------------------------------

    def bias_summary(
        self,
        decision_type: str,
        k: int = DEFAULT_K,
        current_issue_id: str | None = None,
    ) -> str:
        """Retrieve up to ``k`` relevant past entries, formatted for prompt
        injection. Returns the cold-start placeholder when no matching
        entry exists.

        Ranking (no embeddings — simple structured retrieval):

        1. Hard filter on ``decision_type``.
        2. Exclude any entry whose ``issue_id`` equals ``current_issue_id``
           (no self-reference within the same issue).
        3. Sort: passes (``outcome_score >= 0.5``) first, then by recency
           (newer first within each outcome bucket).
        4. Take the top ``k``.
        5. If the result is all passes and any fail exists in the matching
           set, replace the lowest-ranked pass with the most-recent fail
           so the LLM sees at least one "what NOT to repeat" signal.
        6. If the formatted output exceeds ``MAX_BIAS_SUMMARY_TOKENS``
           (rough words * 1.3 estimate), drop entries from the end until
           it fits.
        """
        matching = [
            e for e in self._entries
            if e.decision_type == decision_type
            and (current_issue_id is None or e.issue_id != current_issue_id)
        ]
        if not matching:
            return _COLD_START_PLACEHOLDER

        # Stable sort: passes first, then newer first within each bucket.
        # negate timestamp so larger (newer) sorts first.
        ranked = sorted(
            matching,
            key=lambda e: (0 if e.outcome_score >= 0.5 else 1, -e.timestamp),
        )
        selected = ranked[:k]

        # Failed-entry inclusion rule: if all selected are passes and there
        # is a fail in the matching set, swap the lowest-ranked pass for
        # the most-recent fail.
        all_pass = all(e.outcome_score >= 0.5 for e in selected)
        fails = [e for e in matching if e.outcome_score < 0.5]
        if all_pass and fails and len(selected) > 0:
            most_recent_fail = max(fails, key=lambda e: e.timestamp)
            if most_recent_fail not in selected:
                selected[-1] = most_recent_fail

        # Format and apply token budget.
        formatted = self._format_entries(selected)
        while selected and self._estimate_tokens(formatted) > self.MAX_BIAS_SUMMARY_TOKENS:
            selected = selected[:-1]
            formatted = self._format_entries(selected)

        if not selected:
            return _COLD_START_PLACEHOLDER
        return formatted

    @staticmethod
    def _format_entries(entries: list[SemanticMemoryEntry]) -> str:
        """Format a list of entries as a numbered narrative block.

        Layout is intentionally consistent across entries (entry #, issue,
        outcome marker, winner reason, loser reason) so the LLM can
        pattern-match. Wording is deliberately simple — the surrounding
        hypothesis-gen prompt frames how to read them.
        """
        if not entries:
            return _COLD_START_PLACEHOLDER
        lines = ["Past experiences at this decision type:", ""]
        for i, e in enumerate(entries, 1):
            marker = "✅" if e.outcome_score >= 0.5 else "❌"
            short_issue = e.issue_id.split("__")[-1] if "__" in e.issue_id else e.issue_id
            lines.append(f"[#{i}, issue {short_issue}, outcome {marker}]")
            lines.append(f"Winner: {e.winner_class}. {e.winner_summary}")
            lines.append(f"Losers: {e.counterfactual_summary}")
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough wordcount-to-token estimate (words * 1.3).

        Exact tokenisation would be over-engineering — this is for budget
        capping only, not for billing.
        """
        return int(len(text.split()) * 1.3)

    def entries_for(self, decision_type: str) -> list[SemanticMemoryEntry]:
        return [e for e in self._entries if e.decision_type == decision_type]

    # ------------------------------------------------------------------
    # __novel__ auto-registration (unchanged from numerical version)
    # ------------------------------------------------------------------

    def maybe_register_novel(self, slug: str) -> bool:
        """Record one occurrence of a novel class/decision-type slug.

        Returns True once the slug has been seen
        ``novel_register_threshold`` times — from then on it is a
        first-class registered key.
        """
        if slug in self._registered_novel:
            return True
        self._novel_counter[slug] = self._novel_counter.get(slug, 0) + 1
        registered = self._novel_counter[slug] >= self._novel_register_threshold
        if registered:
            self._registered_novel.add(slug)
            logger.info("SemanticExperienceMemory: registered novel slug %r", slug)
        self.save()
        return registered

    def is_registered_novel(self, slug: str) -> bool:
        return slug in self._registered_novel

    def registered_novels(self) -> list[str]:
        return sorted(self._registered_novel)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Atomically write the memory to disk as JSON (schema v2)."""
        os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
        data = {
            "schema_version": self.SCHEMA_VERSION,
            "entries": [asdict(e) for e in self._entries],
            "novel_counter": self._novel_counter,
            "registered_novel": sorted(self._registered_novel),
        }
        tmp = self._store_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._store_path)

    def load(self) -> None:
        """Load the memory from disk. v1 numerical stores are rejected with a
        warning — fresh ``train_dir`` per protocol means no migration is
        attempted; the in-memory state is reset to empty.
        """
        try:
            with open(self._store_path) as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "SemanticExperienceMemory: failed to load from %s: %s",
                self._store_path, e,
            )
            self._reset()
            return

        version = data.get("schema_version")
        if version != self.SCHEMA_VERSION:
            logger.warning(
                "SemanticExperienceMemory: ignoring %s — schema_version %r "
                "is not v%d (likely an old numerical-advantage store). "
                "Starting fresh; fresh train_dir per eval protocol.",
                self._store_path, version, self.SCHEMA_VERSION,
            )
            self._reset()
            return

        self._entries = [
            SemanticMemoryEntry(**item)
            for item in data.get("entries", [])
        ]
        self._novel_counter = dict(data.get("novel_counter", {}))
        self._registered_novel = set(data.get("registered_novel", []))
        logger.info(
            "SemanticExperienceMemory: loaded %d entries from %s",
            len(self._entries), self._store_path,
        )

    def _reset(self) -> None:
        self._entries = []
        self._novel_counter = {}
        self._registered_novel = set()


# ---------------------------------------------------------------------------
# Backwards-compat alias (issue H3 phase H)
# ---------------------------------------------------------------------------
#
# ``TypedAdvantageMemory`` was the numerical-advantage class this module
# used to export. It is gone — the semantic store completely replaces it
# — but we keep the name resolvable so any out-of-tree caller that still
# imports it gets a DeprecationWarning the first time it touches the
# symbol, instead of an ImportError at startup. The warning is emitted
# from a module-level ``__getattr__`` (PEP 562), not from a bare alias,
# so silent uses are visible to anyone running with ``-Wdefault``.

import warnings  # noqa: E402  — kept next to its only consumer


def __getattr__(name: str):
    if name == "TypedAdvantageMemory":
        warnings.warn(
            "TypedAdvantageMemory was replaced by SemanticExperienceMemory "
            "in issue H3 (semantic experience memory). The numerical-advantage "
            "class no longer exists; this alias resolves to "
            "SemanticExperienceMemory. Update your import: "
            "`from midas_agent.workspace.hta.advantage_memory import "
            "SemanticExperienceMemory`.",
            DeprecationWarning,
            stacklevel=2,
        )
        return SemanticExperienceMemory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
