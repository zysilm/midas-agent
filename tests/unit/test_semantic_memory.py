"""Unit tests for SemanticExperienceMemory (issue H3).

Dependencies are mocked directly with unittest.mock — no Fake/Stub classes.
The class under test is pure data + simple ranking; tests touch it directly.
"""
import json
import os
import tempfile
import time

import pytest

from midas_agent.workspace.hta.advantage_memory import (
    SemanticExperienceMemory,
    SemanticMemoryEntry,
)


def _entry(decision_type="root_cause_localization",
           winner_class="framework_default_value",
           winner_summary="winning lesson",
           counterfactual_summary="losers lesson",
           outcome_score=1.0,
           issue_id="astropy__astropy-1",
           ts=None,
           is_novel=False) -> SemanticMemoryEntry:
    return SemanticMemoryEntry(
        decision_type=decision_type,
        winner_class=winner_class,
        winner_summary=winner_summary,
        counterfactual_summary=counterfactual_summary,
        outcome_score=outcome_score,
        issue_id=issue_id,
        timestamp=ts if ts is not None else time.time(),
        is_novel_winner=is_novel,
    )


@pytest.fixture
def store_path():
    d = tempfile.mkdtemp(prefix="h3_mem_")
    return os.path.join(d, "mem.json")


@pytest.mark.unit
class TestWritePath:
    def test_buffer_and_commit_pending(self, store_path):
        m = SemanticExperienceMemory(store_path)
        m.buffer(_entry(issue_id="i1"))
        m.buffer(_entry(issue_id="i2"))
        m.buffer(_entry(issue_id="i3"))
        m.commit_pending(outcome_score=0.8)
        assert len(m) == 3
        assert all(e.outcome_score == pytest.approx(0.8) for e in m._entries)
        assert m._pending == []

    def test_discard_pending_loses_entries(self, store_path):
        m = SemanticExperienceMemory(store_path)
        m.buffer(_entry())
        m.buffer(_entry())
        m.buffer(_entry())
        m.discard_pending()
        m.commit_pending(outcome_score=1.0)
        assert len(m) == 0

    def test_buffer_rejects_non_entry(self, store_path):
        m = SemanticExperienceMemory(store_path)
        with pytest.raises(TypeError):
            m.buffer("not an entry")


@pytest.mark.unit
class TestPersistence:
    def test_save_load_roundtrip(self, store_path):
        m = SemanticExperienceMemory(store_path)
        for i in range(5):
            m.buffer(_entry(issue_id=f"i{i}"))
        m.commit_pending(outcome_score=0.6)
        m.maybe_register_novel("recursive_bypass")

        loaded = SemanticExperienceMemory(store_path)
        assert len(loaded) == 5
        assert all(e.outcome_score == pytest.approx(0.6) for e in loaded._entries)
        assert loaded._novel_counter.get("recursive_bypass") == 1

    def test_load_rejects_v1_schema(self, store_path, caplog):
        # Write a fake old-format JSON (v1 numerical with "stats", no schema_version).
        with open(store_path, "w") as f:
            json.dump({
                "stats": [{"decision_type": "rcl", "hypothesis_class": "x",
                           "count": 5, "mean": 0.4, "m2": 0.1}],
                "novel_counter": {},
                "registered_novel": [],
            }, f)
        with caplog.at_level("WARNING"):
            m = SemanticExperienceMemory(store_path)
        assert len(m) == 0
        assert any("schema_version" in r.message for r in caplog.records)


@pytest.mark.unit
class TestBiasSummary:
    def test_bias_summary_empty(self, store_path):
        m = SemanticExperienceMemory(store_path)
        out = m.bias_summary("root_cause_localization")
        assert "No prior experience" in out

    def test_bias_summary_filters_decision_type(self, store_path):
        m = SemanticExperienceMemory(store_path)
        for i in range(3):
            m._entries.append(_entry(
                decision_type="root_cause_localization",
                issue_id=f"rcl-{i}", winner_class=f"rcl_class_{i}",
            ))
        for i in range(3):
            m._entries.append(_entry(
                decision_type="fix_locality_scope",
                issue_id=f"fl-{i}", winner_class=f"fl_class_{i}",
            ))
        out = m.bias_summary("root_cause_localization", k=5)
        assert "rcl_class_0" in out
        # No fix_locality classes should leak in.
        for i in range(3):
            assert f"fl_class_{i}" not in out

    def test_bias_summary_excludes_self_issue(self, store_path):
        m = SemanticExperienceMemory(store_path)
        m._entries.append(_entry(issue_id="self_issue", winner_class="A"))
        m._entries.append(_entry(issue_id="other", winner_class="B"))
        out = m.bias_summary("root_cause_localization",
                             current_issue_id="self_issue")
        assert "A" not in out
        assert "B" in out

    def test_bias_summary_prefers_passes_but_includes_one_fail(self, store_path):
        m = SemanticExperienceMemory(store_path)
        now = time.time()
        # 8 passes (older timestamps), 2 fails (one older, one most-recent).
        for i in range(8):
            m._entries.append(_entry(
                issue_id=f"pass-{i}", winner_class=f"pass_class_{i}",
                outcome_score=1.0, ts=now - 100 - i,
            ))
        m._entries.append(_entry(
            issue_id="fail-old", winner_class="fail_old_class",
            outcome_score=0.1, ts=now - 200,
        ))
        m._entries.append(_entry(
            issue_id="fail-recent", winner_class="fail_recent_class",
            outcome_score=0.1, ts=now - 1,
        ))
        out = m.bias_summary("root_cause_localization", k=5)
        # Must include the most-recent fail.
        assert "fail_recent_class" in out
        # Must NOT include the older fail (the inclusion rule injects one,
        # the most recent).
        assert "fail_old_class" not in out
        # The 4 most-recent passes should also be present.
        assert "pass_class_0" in out

    def test_bias_summary_token_budget(self, store_path):
        m = SemanticExperienceMemory(store_path)
        # 30 entries, each with very long summaries.
        big = "word " * 200
        for i in range(30):
            m._entries.append(_entry(
                issue_id=f"issue-{i}", winner_class=f"class_{i}",
                winner_summary=big, counterfactual_summary=big,
            ))
        out = m.bias_summary("root_cause_localization", k=30)
        # Rough token estimate must stay under the cap.
        assert len(out.split()) * 1.3 <= SemanticExperienceMemory.MAX_BIAS_SUMMARY_TOKENS


@pytest.mark.unit
class TestNovelRegistration:
    def test_novel_registers_after_threshold(self, store_path):
        m = SemanticExperienceMemory(store_path, novel_register_threshold=3)
        assert m.maybe_register_novel("async_race") is False
        assert m.maybe_register_novel("async_race") is False
        assert m.maybe_register_novel("async_race") is True
        assert m.is_registered_novel("async_race")
        assert m.maybe_register_novel("async_race") is True  # idempotent
