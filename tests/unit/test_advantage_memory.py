"""Unit tests for TypedAdvantageMemory — HTA typed advantage store."""
import json
import os
import statistics
import tempfile

import pytest

from midas_agent.workspace.hta.advantage_memory import (
    AdvantageStat,
    TypedAdvantageMemory,
)


@pytest.fixture
def store_path():
    d = tempfile.mkdtemp(prefix="hta_mem_")
    yield os.path.join(d, "advantage_memory.json")


@pytest.mark.unit
class TestTypedAdvantageMemory:
    def test_construction_empty(self, store_path):
        mem = TypedAdvantageMemory(store_path)
        assert len(mem) == 0
        assert mem.all_stats() == []

    def test_buffer_and_commit(self, store_path):
        mem = TypedAdvantageMemory(store_path)
        mem.buffer("root_cause_localization", "framework_default_value", 1.0)
        mem.buffer("root_cause_localization", "operator_overload_path", -1.0)
        # Pending is not applied until commit.
        assert len(mem) == 0
        mem.commit_pending(outcome_score=1.0)
        assert len(mem) == 2
        assert mem.stat("root_cause_localization", "framework_default_value").count == 1

    def test_commit_clears_pending(self, store_path):
        mem = TypedAdvantageMemory(store_path)
        mem.buffer("dt", "c", 0.5)
        mem.commit_pending(outcome_score=1.0)
        mem.commit_pending(outcome_score=1.0)  # nothing pending — no-op
        assert mem.stat("dt", "c").count == 1

    def test_discard_pending(self, store_path):
        mem = TypedAdvantageMemory(store_path)
        mem.buffer("dt", "c", 0.5)
        mem.discard_pending()
        mem.commit_pending(outcome_score=1.0)
        assert len(mem) == 0

    def test_asymmetric_update_favours_positive(self, store_path):
        """Equal-magnitude +/- evidence: the mean ends up positive because
        positive advantages step with eta_high and negative with eta_low
        (Clip-Higher analog under EMA)."""
        mem = TypedAdvantageMemory(store_path, eta_high=0.30, eta_low=0.10)
        # Alternating +1 / -1 about a starting mean of 0.
        for adv in [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]:
            mem.buffer("dt", "c", adv)
        mem.commit_pending(outcome_score=1.0)
        assert mem.stat("dt", "c").mean > 0.0

    def test_ema_recovers_from_a_single_lucky_first_sample(self, store_path):
        """The reason Welford-with-step was replaced: under it, the first
        observation had 100% influence and pinned the mean for many later
        updates. Under EMA, a single anomalous sample is absorbed within
        a handful of subsequent observations.
        """
        mem = TypedAdvantageMemory(store_path, eta_high=0.30, eta_low=0.10)
        # One lucky +5.0 spike, then ten neutral 0.0 samples.
        mem.buffer("dt", "c", 5.0)
        for _ in range(10):
            mem.buffer("dt", "c", 0.0)
        mem.commit_pending(outcome_score=1.0)
        # The mean should have drifted well below the initial spike.
        assert mem.stat("dt", "c").mean < 1.0

    def test_default_etas_match_spec(self, store_path):
        """Spec §003 item 4: eta_high=0.30, eta_low=0.10. The defaults must
        not regress — earlier they were 1.0/0.3, which is 10x too large
        and was the root of issue #44 B9.
        """
        mem = TypedAdvantageMemory(store_path)
        # Buffer one positive sample of +1.0 -> mean should move by 0.30.
        mem.buffer("dt", "c", 1.0)
        mem.commit_pending(outcome_score=1.0)
        assert mem.stat("dt", "c").mean == pytest.approx(0.30, abs=1e-9)
        # One negative sample of -1.0 -> mean should move down by 0.10*(-1.0 - 0.30)
        # = -0.13, i.e. to 0.17. (Branches on sign of x, not delta.)
        mem.buffer("dt", "c", -1.0)
        mem.commit_pending(outcome_score=1.0)
        assert mem.stat("dt", "c").mean == pytest.approx(0.17, abs=1e-9)

    def test_outcome_score_weights_update(self, store_path):
        """A failed episode (outcome 0) updates with less weight than a passed one."""
        mem_fail = TypedAdvantageMemory(store_path + ".a")
        mem_pass = TypedAdvantageMemory(store_path + ".b")
        mem_fail.buffer("dt", "c", 1.0)
        mem_fail.commit_pending(outcome_score=0.0)
        mem_pass.buffer("dt", "c", 1.0)
        mem_pass.commit_pending(outcome_score=1.0)
        assert mem_pass.stat("dt", "c").mean > mem_fail.stat("dt", "c").mean

    # The adaptive_g mechanism was removed (issue #44 C1/B1). G is now fixed
    # at 3 per courseware spec §003. The previous adaptive_g tests are gone;
    # the corresponding engine-side behaviour is exercised in
    # tests/integration/test_hta_engine.py.

    def test_novel_registers_after_threshold(self, store_path):
        mem = TypedAdvantageMemory(store_path, novel_register_threshold=3)
        assert mem.maybe_register_novel("async_race") is False
        assert mem.maybe_register_novel("async_race") is False
        assert mem.maybe_register_novel("async_race") is True
        assert mem.is_registered_novel("async_race") is True
        # Already registered — stays True.
        assert mem.maybe_register_novel("async_race") is True

    def test_novel_counts_are_per_slug(self, store_path):
        mem = TypedAdvantageMemory(store_path, novel_register_threshold=2)
        mem.maybe_register_novel("a")
        assert mem.maybe_register_novel("b") is False
        assert mem.maybe_register_novel("a") is True

    def test_bias_summary_cold_and_warm(self, store_path):
        mem = TypedAdvantageMemory(store_path)
        assert "cold start" in mem.bias_summary("dt")
        mem.buffer("dt", "c", 0.5)
        mem.commit_pending(outcome_score=1.0)
        summary = mem.bias_summary("dt")
        assert "c [n=1" in summary

    def test_save_load_round_trip(self, store_path):
        mem = TypedAdvantageMemory(store_path, novel_register_threshold=2)
        mem.buffer("dt", "c", 0.7)
        mem.buffer("dt", "d", -0.3)
        mem.commit_pending(outcome_score=1.0)
        mem.maybe_register_novel("slug_x")

        reloaded = TypedAdvantageMemory(store_path, novel_register_threshold=2)
        assert len(reloaded) == 2
        assert reloaded.stat("dt", "c").mean == pytest.approx(mem.stat("dt", "c").mean)
        assert reloaded._novel_counter.get("slug_x") == 1

    def test_save_writes_valid_json(self, store_path):
        mem = TypedAdvantageMemory(store_path)
        mem.buffer("dt", "c", 0.5)
        mem.commit_pending(outcome_score=1.0)
        with open(store_path) as f:
            data = json.load(f)
        assert "stats" in data and len(data["stats"]) == 1

    def test_load_corrupt_file_resets(self, store_path):
        with open(store_path, "w") as f:
            f.write("{not valid json")
        mem = TypedAdvantageMemory(store_path)
        assert len(mem) == 0


@pytest.mark.unit
class TestAdvantageStat:
    def test_variance_zero_when_single_observation(self):
        stat = AdvantageStat("dt", "c", count=1, mean=0.5, m2=0.0)
        assert stat.variance == 0.0
