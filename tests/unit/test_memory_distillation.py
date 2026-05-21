"""Unit tests for HTAEngine._distill_memory_entry (issue H3).

Construct a real HTAEngine with a MagicMock system_llm so we can control
the tool-call response per case. The engine's other dependencies (call_llm,
actions, run_bash, write_file) are also MagicMocks — distillation logic is
the only behaviour under test here.
"""
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.advantage_memory import (
    SemanticExperienceMemory,
    SemanticMemoryEntry,
)
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry, Hypothesis
from midas_agent.workspace.hta.engine import HTAEngine, HTAEngineConfig


def _text_response():
    return LLMResponse(
        content="done", tool_calls=None,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _distill_response(winner="winner reason", losers="loser reason"):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="d1", name="submit_distillation", arguments={
            "winner_summary": winner,
            "counterfactual_summary": losers,
        })],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _build_engine(system_llm, config=None):
    issue = Issue(issue_id="i1", repo="o/r", description="bug")
    d = tempfile.mkdtemp(prefix="h3_dist_")
    memory = SemanticExperienceMemory(os.path.join(d, "mem.json"))
    engine = HTAEngine(
        issue=issue,
        call_llm=MagicMock(return_value=_text_response()),
        system_llm=system_llm,
        actions=[],
        advantage_memory=memory,
        registry=DecisionPointRegistry(),
        run_bash=MagicMock(return_value="ok"),
        write_file=MagicMock(return_value="/tmp/_hta_probe.py"),
        remove_file=MagicMock(),
        config=config or HTAEngineConfig(),
        work_dir="/tmp/work",
        balance_provider=lambda: 1_000_000,
    )
    return engine


def _make_hypotheses():
    return [
        Hypothesis(name="framework_default_value", rationale="r1",
                   predicted_path="pkg/x.py", test_payload="",
                   score=0.9, advantage=1.0),
        Hypothesis(name="operator_overload_path", rationale="r2",
                   predicted_path="pkg/y.py", test_payload="",
                   score=0.4, advantage=-0.2),
        Hypothesis(name="serialization_roundtrip", rationale="r3",
                   predicted_path="pkg/z.py", test_payload="",
                   score=0.1, advantage=-0.8),
    ]


@pytest.mark.unit
class TestDistillationCall:
    def test_distillation_happy_path(self):
        engine = _build_engine(MagicMock(return_value=_distill_response()))
        dp = engine._registry.get("root_cause_localization")
        hyps = _make_hypotheses()
        entry = engine._distill_memory_entry(dp, hyps, hyps[0])
        assert entry is not None
        assert isinstance(entry, SemanticMemoryEntry)
        assert entry.winner_class == "framework_default_value"
        assert entry.winner_summary == "winner reason"
        assert entry.counterfactual_summary == "loser reason"
        assert engine._memory_distillations_used == 1

    def test_distillation_returns_none_on_no_tool_call(self):
        engine = _build_engine(MagicMock(return_value=_text_response()))
        dp = engine._registry.get("root_cause_localization")
        hyps = _make_hypotheses()
        entry = engine._distill_memory_entry(dp, hyps, hyps[0])
        assert entry is None
        assert engine._memory_distillations_used == 0

    def test_distillation_returns_none_on_malformed_args(self):
        # Empty winner_summary triggers the guard -> None.
        engine = _build_engine(MagicMock(return_value=_distill_response(winner="")))
        dp = engine._registry.get("root_cause_localization")
        hyps = _make_hypotheses()
        entry = engine._distill_memory_entry(dp, hyps, hyps[0])
        assert entry is None
        assert engine._memory_distillations_used == 0

    def test_distillation_cap_enforced(self):
        engine = _build_engine(
            MagicMock(return_value=_distill_response()),
            config=HTAEngineConfig(max_memory_distillations=3),
        )
        dp = engine._registry.get("root_cause_localization")
        hyps = _make_hypotheses()
        # First 3 succeed.
        for _ in range(3):
            assert engine._distill_memory_entry(dp, hyps, hyps[0]) is not None
        assert engine._memory_distillations_used == 3
        # 4th returns None without invoking system_llm.
        engine._system_llm.reset_mock()
        assert engine._distill_memory_entry(dp, hyps, hyps[0]) is None
        engine._system_llm.assert_not_called()

    def test_distillation_failures_dont_block_decision(self):
        # system_llm raises -> distillation returns None silently.
        broken = MagicMock(side_effect=RuntimeError("network down"))
        engine = _build_engine(broken)
        dp = engine._registry.get("root_cause_localization")
        hyps = _make_hypotheses()
        entry = engine._distill_memory_entry(dp, hyps, hyps[0])
        assert entry is None
        # Engine state remains consistent — counter is unchanged.
        assert engine._memory_distillations_used == 0
