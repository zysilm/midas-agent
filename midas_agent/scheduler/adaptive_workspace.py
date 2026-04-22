"""Adaptive workspace controller — 1 workspace normally, 2 during head-to-head.

Only runs 2 workspaces when GEPA produces a different config to compare.
Otherwise runs 1 to save budget. Selects winner by average η over the
head-to-head phase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PhaseStats:
    """Tracks η values for a workspace across a phase."""
    workspace_id: str
    etas: list[float] = field(default_factory=list)

    @property
    def avg_eta(self) -> float:
        return sum(self.etas) / len(self.etas) if self.etas else 0.0


class AdaptiveWorkspaceController:
    """Manages workspace count based on GEPA optimization results.

    Phases:
      - SINGLE: 1 workspace, waiting for GEPA to produce a different config
      - HEAD_TO_HEAD: 2 workspaces (champion vs challenger), competing
        until next GEPA trigger

    At each GEPA trigger boundary:
      - Compare average η over the phase
      - Winner becomes champion
      - If GEPA produced a new config → HEAD_TO_HEAD with new challenger
      - If no change → SINGLE with champion
    """

    SINGLE = "single"
    HEAD_TO_HEAD = "head_to_head"

    def __init__(self) -> None:
        self.phase = self.SINGLE
        self.champion_stats: PhaseStats | None = None
        self.challenger_stats: PhaseStats | None = None

    @property
    def active_count(self) -> int:
        """Number of workspaces to run."""
        return 2 if self.phase == self.HEAD_TO_HEAD else 1

    def init_champion(self, workspace_id: str) -> None:
        """Set the initial champion (first workspace created)."""
        self.champion_stats = PhaseStats(workspace_id=workspace_id)
        logger.info("Adaptive: champion = %s (phase: SINGLE)", workspace_id)

    def record_episode(self, workspace_id: str, eta: float) -> None:
        """Record η for a workspace after an episode."""
        if self.champion_stats and workspace_id == self.champion_stats.workspace_id:
            self.champion_stats.etas.append(eta)
        elif self.challenger_stats and workspace_id == self.challenger_stats.workspace_id:
            self.challenger_stats.etas.append(eta)

    def on_gepa_result(
        self,
        champion_changed: bool,
        challenger_changed: bool | None = None,
    ) -> dict:
        """Called after GEPA optimization. Decides phase transition.

        Args:
            champion_changed: True if GEPA produced a different config for champion
            challenger_changed: True if GEPA changed challenger (None if no challenger)

        Returns:
            dict with keys:
              - "action": "stay_single" | "start_h2h" | "select_winner"
              - "winner_id": workspace_id of winner (for select_winner)
              - "loser_id": workspace_id of loser (for select_winner)
        """
        if self.phase == self.SINGLE:
            if champion_changed:
                # Champion's config changed — need a challenger to compare
                logger.info("Adaptive: GEPA changed champion config → HEAD_TO_HEAD")
                return {"action": "start_h2h"}
            else:
                logger.info("Adaptive: GEPA no change → stay SINGLE")
                return {"action": "stay_single"}

        else:  # HEAD_TO_HEAD
            # Select winner by average η
            champ_avg = self.champion_stats.avg_eta if self.champion_stats else 0
            chall_avg = self.challenger_stats.avg_eta if self.challenger_stats else 0

            if champ_avg >= chall_avg:
                winner = self.champion_stats
                loser = self.challenger_stats
            else:
                winner = self.challenger_stats
                loser = self.champion_stats

            winner_id = winner.workspace_id if winner else ""
            loser_id = loser.workspace_id if loser else ""

            logger.info(
                "Adaptive: phase end — champion η=%.6f, challenger η=%.6f → winner=%s",
                champ_avg, chall_avg, winner_id,
            )

            # Count how many configs changed
            changes = sum(
                1 for c in [champion_changed, challenger_changed]
                if c is True
            )

            if changes == 0:
                # Neither changed → kill loser, go SINGLE
                self.champion_stats = PhaseStats(workspace_id=winner_id)
                self.challenger_stats = None
                self.phase = self.SINGLE
                logger.info("Adaptive: 0 changes → SINGLE with %s", winner_id)
                return {
                    "action": "select_winner",
                    "winner_id": winner_id,
                    "loser_id": loser_id,
                    "next_phase": self.SINGLE,
                }

            elif changes == 1:
                # One changed → winner stays, changed one becomes challenger
                self.champion_stats = PhaseStats(workspace_id=winner_id)
                self.challenger_stats = PhaseStats(workspace_id=loser_id)
                self.phase = self.HEAD_TO_HEAD
                logger.info(
                    "Adaptive: 1 change → HEAD_TO_HEAD (%s vs %s)",
                    winner_id, loser_id,
                )
                return {
                    "action": "select_winner",
                    "winner_id": winner_id,
                    "loser_id": loser_id,
                    "next_phase": self.HEAD_TO_HEAD,
                }

            else:
                # Both changed → keep the better two
                self.champion_stats = PhaseStats(workspace_id=winner_id)
                self.challenger_stats = PhaseStats(workspace_id=loser_id)
                self.phase = self.HEAD_TO_HEAD
                logger.info(
                    "Adaptive: 2 changes → HEAD_TO_HEAD (%s vs %s)",
                    winner_id, loser_id,
                )
                return {
                    "action": "continue_h2h",
                    "winner_id": winner_id,
                    "loser_id": loser_id,
                }

    def start_head_to_head(self, challenger_id: str) -> None:
        """Enter head-to-head phase with a new challenger."""
        self.challenger_stats = PhaseStats(workspace_id=challenger_id)
        # Reset champion stats for the new phase
        if self.champion_stats:
            self.champion_stats.etas = []
        self.phase = self.HEAD_TO_HEAD
        logger.info(
            "Adaptive: HEAD_TO_HEAD started — %s vs %s",
            self.champion_stats.workspace_id if self.champion_stats else "?",
            challenger_id,
        )

    def to_dict(self) -> dict:
        """Serialize state for checkpoint persistence."""
        return {
            "phase": self.phase,
            "champion": {
                "workspace_id": self.champion_stats.workspace_id,
                "etas": self.champion_stats.etas,
            } if self.champion_stats else None,
            "challenger": {
                "workspace_id": self.challenger_stats.workspace_id,
                "etas": self.challenger_stats.etas,
            } if self.challenger_stats else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AdaptiveWorkspaceController":
        """Restore from checkpoint data."""
        ctrl = cls()
        ctrl.phase = data.get("phase", cls.SINGLE)
        champ = data.get("champion")
        if champ:
            ctrl.champion_stats = PhaseStats(
                workspace_id=champ["workspace_id"],
                etas=champ.get("etas", []),
            )
        chall = data.get("challenger")
        if chall:
            ctrl.challenger_stats = PhaseStats(
                workspace_id=chall["workspace_id"],
                etas=chall.get("etas", []),
            )
        return ctrl
