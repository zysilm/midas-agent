"""Unit tests for DecisionPointMetaJudge.

The system LLM is a MagicMock returning real LLMResponse objects — no fakes.
"""
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage

from midas_agent.workspace.hta.decision_point import DecisionPointRegistry, RuleTriggerInputs
from midas_agent.workspace.hta.meta_judge import DecisionPointMetaJudge


def _verdict(**kw):
    args = {
        "is_decision_point": kw.get("is_decision_point", True),
        "decision_type": kw.get("decision_type", "investigation_continuation"),
        "path_dependency": kw.get("path_dependency", True),
        "enumerable_alternatives": kw.get("enumerable_alternatives", True),
        "delayed_verification": kw.get("delayed_verification", True),
        "reason": kw.get("reason", "because"),
    }
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="judge_decision_point", arguments=args)],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


@pytest.fixture
def registry():
    return DecisionPointRegistry()


@pytest.mark.unit
class TestMetaJudgeRulePreFilter:
    def test_issue_start_returns_rcl_without_llm(self, registry):
        llm = MagicMock()
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        dp = judge.classify(RuleTriggerInputs(is_issue_start=True), is_stuck=False)
        assert dp.decision_type == "root_cause_localization"
        llm.assert_not_called()

    def test_not_stuck_and_no_rule_returns_none_without_llm(self, registry):
        llm = MagicMock()
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        dp = judge.classify(RuleTriggerInputs(), is_stuck=False)
        assert dp is None
        llm.assert_not_called()


@pytest.mark.unit
class TestMetaJudgeLLM:
    def test_stuck_state_all_criteria_returns_continuation(self, registry):
        llm = MagicMock(return_value=_verdict())
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        dp = judge.classify(
            RuleTriggerInputs(), is_stuck=True,
            issue_summary="x", recent_trace="agent looping",
        )
        assert dp.decision_type == "investigation_continuation"
        llm.assert_called_once()

    def test_missing_one_criterion_returns_none(self, registry):
        llm = MagicMock(return_value=_verdict(delayed_verification=False))
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        dp = judge.classify(RuleTriggerInputs(), is_stuck=True, recent_trace="t")
        assert dp is None

    def test_is_decision_point_false_returns_none(self, registry):
        llm = MagicMock(return_value=_verdict(is_decision_point=False))
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        assert judge.classify(RuleTriggerInputs(), is_stuck=True, recent_trace="t") is None

    def test_novel_decision_type_synthesised(self, registry):
        llm = MagicMock(return_value=_verdict(decision_type="__novel__:dependency_pin"))
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        dp = judge.classify(RuleTriggerInputs(), is_stuck=True, recent_trace="t")
        assert dp.decision_type == "__novel__:dependency_pin"
        assert dp.trigger_kind == "meta_judge"

    def test_api_error_returns_none(self, registry):
        llm = MagicMock(side_effect=RuntimeError("api down"))
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        assert judge.classify(RuleTriggerInputs(), is_stuck=True, recent_trace="t") is None

    def test_no_tool_call_returns_none(self, registry):
        resp = LLMResponse(
            content="prose", tool_calls=None,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
        llm = MagicMock(return_value=resp)
        judge = DecisionPointMetaJudge(system_llm=llm, registry=registry)
        assert judge.classify(RuleTriggerInputs(), is_stuck=True, recent_trace="t") is None
