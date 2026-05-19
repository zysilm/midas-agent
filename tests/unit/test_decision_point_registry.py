"""Unit tests for the HTA DecisionPointRegistry and hypothesis model."""
import pytest

from midas_agent.workspace.hta.decision_point import (
    DecisionPointRegistry,
    Hypothesis,
    RuleTriggerInputs,
)


@pytest.mark.unit
class TestDecisionPointRegistry:
    def test_four_seed_dps_by_default(self):
        reg = DecisionPointRegistry()
        types = set(reg.decision_types())
        assert types == {
            "root_cause_localization",
            "fix_locality_scope",
            "spec_interpretation",
            "investigation_continuation",
        }
        assert reg.test_scope_enabled is False

    def test_test_scope_dp_added_when_enabled(self):
        reg = DecisionPointRegistry(enable_test_scope=True)
        assert "test_scope_strategy" in reg.decision_types()
        assert len(reg.all()) == 5

    def test_rcl_uses_rule_trigger_and_tier0(self):
        reg = DecisionPointRegistry()
        rcl = reg.get("root_cause_localization")
        assert rcl.trigger_kind == "rule"
        assert rcl.verifier_tier == 0
        assert "framework_default_value" in rcl.seed_classes

    def test_spec_interpretation_is_the_only_tier2_dp(self):
        reg = DecisionPointRegistry()
        tier2 = [dp.decision_type for dp in reg.all() if dp.allow_tier2]
        assert tier2 == ["spec_interpretation"]

    def test_investigation_continuation_uses_meta_judge(self):
        reg = DecisionPointRegistry()
        assert reg.get("investigation_continuation").trigger_kind == "meta_judge"

    def test_rule_triggered_issue_start_is_rcl(self):
        reg = DecisionPointRegistry()
        dp = reg.rule_triggered(RuleTriggerInputs(is_issue_start=True))
        assert dp.decision_type == "root_cause_localization"

    def test_rule_triggered_all_negative_is_spec_interpretation(self):
        reg = DecisionPointRegistry()
        dp = reg.rule_triggered(RuleTriggerInputs(rcl_all_negative=True))
        assert dp.decision_type == "spec_interpretation"

    def test_rule_triggered_first_code_mod_is_fix_locality(self):
        reg = DecisionPointRegistry()
        dp = reg.rule_triggered(
            RuleTriggerInputs(rcl_winner_found=True, about_to_modify_code=True)
        )
        assert dp.decision_type == "fix_locality_scope"

    def test_rule_triggered_none_when_no_condition(self):
        reg = DecisionPointRegistry()
        assert reg.rule_triggered(RuleTriggerInputs()) is None

    def test_rule_triggered_test_scope_only_when_enabled(self):
        off = DecisionPointRegistry(enable_test_scope=False)
        assert off.rule_triggered(RuleTriggerInputs(test_scope_pending=True)) is None
        on = DecisionPointRegistry(enable_test_scope=True)
        dp = on.rule_triggered(RuleTriggerInputs(test_scope_pending=True))
        assert dp.decision_type == "test_scope_strategy"

    def test_get_unknown_returns_none(self):
        assert DecisionPointRegistry().get("nonexistent") is None


@pytest.mark.unit
class TestHypothesis:
    def test_seed_class_is_not_novel(self):
        h = Hypothesis(name="serialization_roundtrip")
        assert h.is_novel is False
        assert h.novel_slug is None

    def test_novel_hypothesis_exposes_slug(self):
        h = Hypothesis(name="__novel__:async_race")
        assert h.is_novel is True
        assert h.novel_slug == "async_race"

    def test_novel_without_slug_falls_back(self):
        h = Hypothesis(name="__novel__")
        assert h.is_novel is True
        assert h.novel_slug == "unspecified"
