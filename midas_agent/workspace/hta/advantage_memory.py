"""TypedAdvantageMemory — persistent typed memory of hypothesis advantages.

Each decision point produces a group-relative advantage for every hypothesis
(winner and losers). Those advantages are accumulated here, keyed by
``(decision_type, hypothesis_class)``, as running statistics. The memory then
biases future hypothesis generation and drives adaptive G — when one class is a
decisive favourite, G collapses to 1 and the decision point degenerates to
plain ReAct (zero overhead).

Knowledge transfers across issues by *structural* key, not by surface text
similarity: a serialization-roundtrip bug in astropy and one in django share
the same ``(root_cause_localization, serialization_roundtrip)`` row.

Persistence mirrors LessonStore: atomic JSON via ``.tmp`` + ``os.replace``,
loaded on construction. The per-episode ``_pending`` buffer is never persisted —
it is applied (and cleared) by ``commit_pending()`` in ``post_episode``, so a
crashed episode cannot poison the memory.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AdvantageStat:
    """Running statistics for one (decision_type, hypothesis_class) cell.

    ``mean``/``m2`` are maintained by an asymmetric variant of Welford's
    online algorithm (see TypedAdvantageMemory._update).
    """

    decision_type: str
    hypothesis_class: str
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    @property
    def variance(self) -> float:
        return self.m2 / self.count if self.count > 1 else 0.0


class TypedAdvantageMemory:
    """Persistent store of typed hypothesis advantages.

    Shared across all workspaces and episodes in a run — constructed once and
    handed to every HTAWorkspace, which is what makes the cold-to-warm
    transition meaningful.
    """

    def __init__(
        self,
        store_path: str,
        epsilon: float = 1e-6,
        novel_register_threshold: int = 3,
        clip_higher: float = 1.0,
        clip_lower: float = 0.3,
    ) -> None:
        self._store_path = store_path
        self._epsilon = epsilon
        self._novel_register_threshold = novel_register_threshold
        # Asymmetric step sizes (Clip-Higher analog): raising a class is easier
        # than overturning an established one, so clip_higher > clip_lower.
        self._clip_higher = clip_higher
        self._clip_lower = clip_lower

        self._stats: dict[tuple[str, str], AdvantageStat] = {}
        # (decision_type, hypothesis_class, advantage) buffered within an episode.
        self._pending: list[tuple[str, str, float]] = []
        # Slug -> times seen, for __novel__ auto-registration.
        self._novel_counter: dict[str, int] = {}
        self._registered_novel: set[str] = set()

        if os.path.isfile(store_path):
            self.load()

    def __len__(self) -> int:
        return len(self._stats)

    # ------------------------------------------------------------------
    # Mid-episode buffering / post-episode commit
    # ------------------------------------------------------------------

    def buffer(self, decision_type: str, hypothesis_class: str, advantage: float) -> None:
        """Buffer one hypothesis' advantage. Called by the engine per hypothesis."""
        self._pending.append((decision_type, hypothesis_class, advantage))

    def commit_pending(self, outcome_score: float) -> None:
        """Apply all buffered advantages, weighted by the episode outcome.

        ``outcome_score`` is the episode's execution score (s_exec, in [0, 1]).
        A failed episode still updates the memory but with reduced weight, so a
        hypothesis that won its decision point yet led nowhere is not over-
        rewarded. Called once from HTAWorkspace.post_episode.
        """
        outcome_weight = 0.5 + 0.5 * max(0.0, min(1.0, outcome_score))
        for decision_type, hypothesis_class, advantage in self._pending:
            self._update(decision_type, hypothesis_class, advantage * outcome_weight)
        n = len(self._pending)
        self._pending.clear()
        if n:
            self.save()
            logger.info(
                "TypedAdvantageMemory: committed %d advantages (outcome=%.2f)",
                n, outcome_score,
            )

    def _update(self, decision_type: str, hypothesis_class: str, x: float) -> None:
        """Asymmetric Welford update for one cell."""
        key = (decision_type, hypothesis_class)
        stat = self._stats.get(key)
        if stat is None:
            stat = AdvantageStat(decision_type=decision_type, hypothesis_class=hypothesis_class)
            self._stats[key] = stat

        stat.count += 1
        delta = x - stat.mean
        # Positive evidence moves the mean farther than equal-magnitude
        # negative evidence — the Clip-Higher analog.
        step = self._clip_higher if x > stat.mean else self._clip_lower
        stat.mean += step * delta / stat.count
        delta2 = x - stat.mean
        stat.m2 += delta * delta2

    def discard_pending(self) -> None:
        """Drop buffered advantages without applying them (e.g. crashed episode)."""
        self._pending.clear()

    # ------------------------------------------------------------------
    # Generation bias
    # ------------------------------------------------------------------

    # Note: an `adaptive_g` mechanism that returned G ∈ {1, 2, 3} based on
    # historical margin was removed (issue #44, C1/B1). It produced a
    # permanent G=1 absorbing state: once any class crossed the margin
    # threshold on noisy early data, G collapsed to 1 forever for that
    # decision type, and no further memory updates could happen. The
    # courseware spec §003 calls for fixed G=3 with one slot reserved for
    # exploration; HTAEngine now honours that directly.

    def bias_summary(self, decision_type: str) -> str:
        """Human-readable priors block injected into the hypothesis-gen prompt."""
        stats = [s for s in self._stats.values() if s.decision_type == decision_type]
        if not stats:
            return "No historical data yet for this decision type (cold start)."
        stats.sort(key=lambda s: s.mean, reverse=True)
        parts = [
            f"{s.hypothesis_class} [n={s.count}, A={s.mean:+.2f}]"
            for s in stats
        ]
        return "Historical priors (count, mean advantage): " + "; ".join(parts)

    # ------------------------------------------------------------------
    # __novel__ auto-registration
    # ------------------------------------------------------------------

    def maybe_register_novel(self, slug: str) -> bool:
        """Record one occurrence of a novel class/decision-type slug.

        Returns True once the slug has been seen ``novel_register_threshold``
        times — from then on it is a first-class registered key.
        """
        if slug in self._registered_novel:
            return True
        self._novel_counter[slug] = self._novel_counter.get(slug, 0) + 1
        registered = self._novel_counter[slug] >= self._novel_register_threshold
        if registered:
            self._registered_novel.add(slug)
            logger.info("TypedAdvantageMemory: registered novel slug %r", slug)
        self.save()
        return registered

    def is_registered_novel(self, slug: str) -> bool:
        return slug in self._registered_novel

    def registered_novels(self) -> list[str]:
        return sorted(self._registered_novel)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def stat(self, decision_type: str, hypothesis_class: str) -> AdvantageStat | None:
        return self._stats.get((decision_type, hypothesis_class))

    def all_stats(self) -> list[AdvantageStat]:
        return list(self._stats.values())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Atomically write the memory to disk as JSON."""
        os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
        data = {
            "stats": [
                {
                    "decision_type": s.decision_type,
                    "hypothesis_class": s.hypothesis_class,
                    "count": s.count,
                    "mean": s.mean,
                    "m2": s.m2,
                }
                for s in self._stats.values()
            ],
            "novel_counter": self._novel_counter,
            "registered_novel": sorted(self._registered_novel),
        }
        tmp = self._store_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._store_path)

    def load(self) -> None:
        """Load the memory from disk; reset to empty on any failure."""
        try:
            with open(self._store_path) as f:
                data = json.load(f)
            self._stats = {}
            for item in data.get("stats", []):
                stat = AdvantageStat(
                    decision_type=item["decision_type"],
                    hypothesis_class=item["hypothesis_class"],
                    count=item.get("count", 0),
                    mean=item.get("mean", 0.0),
                    m2=item.get("m2", 0.0),
                )
                self._stats[(stat.decision_type, stat.hypothesis_class)] = stat
            self._novel_counter = dict(data.get("novel_counter", {}))
            self._registered_novel = set(data.get("registered_novel", []))
            logger.info(
                "TypedAdvantageMemory: loaded %d cells from %s",
                len(self._stats), self._store_path,
            )
        except Exception as e:
            logger.warning(
                "TypedAdvantageMemory: failed to load from %s: %s", self._store_path, e,
            )
            self._stats = {}
            self._novel_counter = {}
            self._registered_novel = set()
