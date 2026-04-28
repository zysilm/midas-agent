"""Unit tests for AdaptiveWorkspaceController."""
import pytest

from midas_agent.scheduler.adaptive_workspace import (
    AdaptiveWorkspaceController,
    PhaseStats,
)


@pytest.mark.unit
class TestPhaseStats:
    def test_avg_score_empty(self):
        stats = PhaseStats(workspace_id="ws-0")
        assert stats.avg_score == 0.0

    def test_avg_score(self):
        stats = PhaseStats(workspace_id="ws-0", scores=[0.1, 0.2, 0.3])
        assert pytest.approx(stats.avg_score) == 0.2


@pytest.mark.unit
class TestAdaptiveWorkspaceController:
    def test_initial_state(self):
        ctrl = AdaptiveWorkspaceController()
        assert ctrl.phase == "single"
        assert ctrl.active_count == 1

    def test_init_champion(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        assert ctrl.champion_stats.workspace_id == "ws-0"

    def test_record_episode_champion(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.record_episode("ws-0", 0.5)
        assert ctrl.champion_stats.scores == [0.5]

    def test_record_episode_challenger(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        ctrl.record_episode("ws-1", 0.3)
        assert ctrl.challenger_stats.scores == [0.3]

    def test_record_episode_unknown_ignored(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.record_episode("ws-unknown", 0.5)
        assert ctrl.champion_stats.scores == []

    # --- Phase transitions ---

    def test_single_no_change_stays_single(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        result = ctrl.on_gepa_result(champion_changed=False)
        assert result["action"] == "stay_single"
        assert ctrl.phase == "single"
        assert ctrl.active_count == 1

    def test_single_change_starts_h2h(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        result = ctrl.on_gepa_result(champion_changed=True)
        assert result["action"] == "start_h2h"

    def test_start_head_to_head(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        assert ctrl.phase == "head_to_head"
        assert ctrl.active_count == 2
        assert ctrl.challenger_stats.workspace_id == "ws-1"

    def test_h2h_zero_changes_selects_winner_goes_single(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        # Champion does better
        ctrl.record_episode("ws-0", 0.8)
        ctrl.record_episode("ws-1", 0.3)
        result = ctrl.on_gepa_result(
            champion_changed=False, challenger_changed=False
        )
        assert result["action"] == "select_winner"
        assert result["winner_id"] == "ws-0"
        assert result["loser_id"] == "ws-1"
        assert result["next_phase"] == "single"
        assert ctrl.phase == "single"

    def test_h2h_challenger_wins(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        # Challenger does better
        ctrl.record_episode("ws-0", 0.2)
        ctrl.record_episode("ws-1", 0.9)
        result = ctrl.on_gepa_result(
            champion_changed=False, challenger_changed=False
        )
        assert result["winner_id"] == "ws-1"
        assert result["loser_id"] == "ws-0"

    def test_h2h_one_change_continues_h2h(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        ctrl.record_episode("ws-0", 0.5)
        ctrl.record_episode("ws-1", 0.4)
        result = ctrl.on_gepa_result(
            champion_changed=True, challenger_changed=False
        )
        assert result["action"] == "select_winner"
        assert result["next_phase"] == "head_to_head"
        assert ctrl.phase == "head_to_head"

    def test_h2h_both_changed_continues_h2h(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        ctrl.record_episode("ws-0", 0.6)
        ctrl.record_episode("ws-1", 0.4)
        result = ctrl.on_gepa_result(
            champion_changed=True, challenger_changed=True
        )
        assert result["action"] == "continue_h2h"
        assert ctrl.phase == "head_to_head"

    def test_h2h_resets_scores_for_new_phase(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.record_episode("ws-0", 0.5)
        ctrl.start_head_to_head("ws-1")
        # Champion scores should be reset for the new phase
        assert ctrl.champion_stats.scores == []

    def test_multiple_episodes_avg(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        for _ in range(5):
            ctrl.record_episode("ws-0", 0.6)
            ctrl.record_episode("ws-1", 0.4)
        assert pytest.approx(ctrl.champion_stats.avg_score) == 0.6
        assert pytest.approx(ctrl.challenger_stats.avg_score) == 0.4

    def test_full_lifecycle(self):
        """Single → H2H → Single → H2H full cycle."""
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")

        # Phase 1: SINGLE, GEPA no change
        result = ctrl.on_gepa_result(champion_changed=False)
        assert result["action"] == "stay_single"
        assert ctrl.active_count == 1

        # Phase 1: SINGLE, GEPA changes → H2H
        result = ctrl.on_gepa_result(champion_changed=True)
        assert result["action"] == "start_h2h"
        ctrl.start_head_to_head("ws-1")
        assert ctrl.active_count == 2

        # Phase 2: H2H, run some episodes
        ctrl.record_episode("ws-0", 0.7)
        ctrl.record_episode("ws-1", 0.5)
        ctrl.record_episode("ws-0", 0.8)
        ctrl.record_episode("ws-1", 0.4)

        # Phase 3: GEPA, no changes → select winner, go SINGLE
        result = ctrl.on_gepa_result(
            champion_changed=False, challenger_changed=False
        )
        assert result["action"] == "select_winner"
        assert result["winner_id"] == "ws-0"
        assert ctrl.phase == "single"
        assert ctrl.active_count == 1

    # --- Serialization ---

    def test_to_dict_single(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.record_episode("ws-0", 0.5)
        d = ctrl.to_dict()
        assert d["phase"] == "single"
        assert d["champion"]["workspace_id"] == "ws-0"
        assert d["champion"]["scores"] == [0.5]
        assert d["challenger"] is None

    def test_to_dict_h2h(self):
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        ctrl.record_episode("ws-0", 0.6)
        ctrl.record_episode("ws-1", 0.4)
        d = ctrl.to_dict()
        assert d["phase"] == "head_to_head"
        assert d["champion"]["workspace_id"] == "ws-0"
        assert d["challenger"]["workspace_id"] == "ws-1"
        assert d["challenger"]["scores"] == [0.4]

    def test_from_dict_single(self):
        data = {
            "phase": "single",
            "champion": {"workspace_id": "ws-0", "scores": [0.3, 0.7]},
            "challenger": None,
        }
        ctrl = AdaptiveWorkspaceController.from_dict(data)
        assert ctrl.phase == "single"
        assert ctrl.active_count == 1
        assert ctrl.champion_stats.workspace_id == "ws-0"
        assert ctrl.champion_stats.scores == [0.3, 0.7]
        assert ctrl.challenger_stats is None

    def test_from_dict_h2h(self):
        data = {
            "phase": "head_to_head",
            "champion": {"workspace_id": "ws-0", "scores": [0.6]},
            "challenger": {"workspace_id": "ws-1", "scores": [0.4]},
        }
        ctrl = AdaptiveWorkspaceController.from_dict(data)
        assert ctrl.phase == "head_to_head"
        assert ctrl.active_count == 2
        assert ctrl.challenger_stats.workspace_id == "ws-1"

    def test_roundtrip(self):
        """to_dict → from_dict preserves all state."""
        ctrl = AdaptiveWorkspaceController()
        ctrl.init_champion("ws-0")
        ctrl.start_head_to_head("ws-1")
        ctrl.record_episode("ws-0", 0.8)
        ctrl.record_episode("ws-0", 0.6)
        ctrl.record_episode("ws-1", 0.5)

        restored = AdaptiveWorkspaceController.from_dict(ctrl.to_dict())
        assert restored.phase == ctrl.phase
        assert restored.champion_stats.workspace_id == "ws-0"
        assert restored.champion_stats.scores == [0.8, 0.6]
        assert restored.challenger_stats.workspace_id == "ws-1"
        assert restored.challenger_stats.scores == [0.5]
