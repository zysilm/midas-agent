"""Tests for FixLocality anti-gaming detection (issue H1 D2).

These tests exercise the engine's _resolve gaming check by running a
controlled HTAEngine end-to-end: the verifier for fix_locality_scope is
replaced with a MagicMock returning the scores we want. Dependencies are
mocked directly with unittest.mock — no Fake/Stub classes.
"""
import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.advantage_memory import TypedAdvantageMemory
from midas_agent.workspace.hta.analysis.episode_summary import build_summary
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.engine import HTAEngine, HTAEngineConfig
from midas_agent.workspace.hta.graph import NodeKind
from midas_agent.workspace.hta.sub_verifier import SubVerifier


_SEED_CLASSES = {
    "root_cause_localization": [
        "framework_default_value", "operator_overload_path", "serialization_roundtrip",
    ],
    "fix_locality_scope": ["surface_patch", "intermediate_layer", "root_layer"],
}


def _hyp_dict(cls):
    return {
        "hypothesis_class": cls,
        "rationale": f"rationale for {cls}",
        "predicted_path": f"pkg/{cls}.py",
        "test_payload": "assert True, 'HTA_LAYER_HIT'",
    }


def _hyp_response(classes):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(
            id="t1", name="submit_hypotheses",
            arguments={"hypotheses": [_hyp_dict(c) for c in classes]},
        )],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _text_response():
    return LLMResponse(
        content="done", tool_calls=None,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _make_system_llm():
    def system_llm(req):
        tool_names = {t["function"]["name"] for t in (req.tools or [])}
        user = req.messages[-1]["content"] if req.messages else ""
        if "submit_hypotheses" in tool_names:
            m = re.search(r"type:\s*(\S+)", user)
            dt = m.group(1) if m else "root_cause_localization"
            classes = _SEED_CLASSES.get(dt, ["a", "b", "c"])
            return _hyp_response(classes)
        return _text_response()
    return system_llm


def _scored_verifier(score_map, default=0.5):
    v = MagicMock(spec=SubVerifier)
    v.verify.side_effect = lambda h, ctx, cheap=True: score_map.get(h.name, default)
    return v


def _build_engine(memory, fl_verifier):
    issue = Issue(issue_id="i1", repo="o/r", description="the widget crashes")
    # RCL distinct so we never trigger escalation; only FL is the focus.
    rcl_distinct = _scored_verifier({
        "framework_default_value": 0.9, "operator_overload_path": 0.5,
        "serialization_roundtrip": 0.1,
    })
    engine = HTAEngine(
        issue=issue,
        call_llm=MagicMock(return_value=_text_response()),
        system_llm=_make_system_llm(),
        actions=[],
        advantage_memory=memory,
        registry=DecisionPointRegistry(),
        run_bash=MagicMock(return_value="ok"),
        write_file=MagicMock(return_value="/tmp/_hta_probe.py"),
        remove_file=MagicMock(),
        config=HTAEngineConfig(),
        work_dir="/tmp/work",
        balance_provider=lambda: 1_000_000,
    )
    engine._verifiers["root_cause_localization"] = rcl_distinct
    engine._verifiers["fix_locality_scope"] = fl_verifier
    return engine


def _fl_node(graph):
    for n in graph.nodes.values():
        if n.kind == NodeKind.DECISION and n.decision_type == "fix_locality_scope":
            return n
    return None


@pytest.fixture
def memory():
    d = tempfile.mkdtemp(prefix="hta_gaming_")
    return TypedAdvantageMemory(os.path.join(d, "mem.json"))


@pytest.mark.unit
class TestGamingDetection:
    def test_gaming_detected_when_all_score_one(self, memory):
        # All three FL probes score 1.0 — gaming. Engine demotes all to 0.4.
        gamed = _scored_verifier({
            "surface_patch": 1.0, "intermediate_layer": 1.0, "root_layer": 1.0,
        })
        engine = _build_engine(memory, gamed)
        graph = engine.run()
        fl = _fl_node(graph)
        assert fl is not None
        assert fl.payload.get("gaming_detected") is True
        for h in fl.payload["hypotheses"]:
            assert h["score"] == pytest.approx(0.4)

    def test_no_gaming_when_scores_mixed(self, memory):
        # Scores (1.0, 0.3, 0.2) — legitimate discrimination.
        mixed = _scored_verifier({
            "surface_patch": 1.0, "intermediate_layer": 0.3, "root_layer": 0.2,
        })
        engine = _build_engine(memory, mixed)
        graph = engine.run()
        fl = _fl_node(graph)
        assert fl is not None
        assert fl.payload.get("gaming_detected") is False
        scores_by_name = {h["name"]: h["score"] for h in fl.payload["hypotheses"]}
        assert scores_by_name["surface_patch"] == pytest.approx(1.0)
        assert scores_by_name["intermediate_layer"] == pytest.approx(0.3)
        assert scores_by_name["root_layer"] == pytest.approx(0.2)

    def test_no_gaming_when_two_at_one_one_lower(self, memory):
        # Scores (1.0, 1.0, 0.5) — partial discrimination, not gaming.
        partial = _scored_verifier({
            "surface_patch": 1.0, "intermediate_layer": 1.0, "root_layer": 0.5,
        })
        engine = _build_engine(memory, partial)
        graph = engine.run()
        fl = _fl_node(graph)
        assert fl is not None
        assert fl.payload.get("gaming_detected") is False
        scores_by_name = {h["name"]: h["score"] for h in fl.payload["hypotheses"]}
        assert scores_by_name["root_layer"] == pytest.approx(0.5)


@pytest.mark.unit
class TestGamingPayloadPropagation:
    def test_gaming_payload_propagates(self, memory):
        # Same as test_gaming_detected_when_all_score_one but asserting the
        # exact payload structure the analyzer downstream relies on.
        gamed = _scored_verifier({
            "surface_patch": 1.0, "intermediate_layer": 1.0, "root_layer": 1.0,
        })
        engine = _build_engine(memory, gamed)
        graph = engine.run()
        fl = _fl_node(graph)
        assert isinstance(fl.payload, dict)
        assert fl.payload["gaming_detected"] is True

    def test_episode_summary_counts_gaming(self, memory):
        # Run two RCL/FL pairs by running the engine twice on a fresh
        # graph: first issue gamed, second clean. Each run produces its
        # own graph; we count gaming via the summary for the gamed graph.
        gamed = _scored_verifier({
            "surface_patch": 1.0, "intermediate_layer": 1.0, "root_layer": 1.0,
        })
        engine = _build_engine(memory, gamed)
        graph = engine.run()
        summary = build_summary(
            issue_id="i1", branch="t", graph=graph,
            initial_budget=1_000_000, final_budget=999_000,
            tier2_calls_used=0,
        )
        fl_section = summary["fix_locality"]
        assert fl_section["gaming_detected"] is True
        assert fl_section["gaming_detected_count"] == 1

        # Clean run produces gaming_detected_count == 0.
        clean = _scored_verifier({
            "surface_patch": 0.9, "intermediate_layer": 0.4, "root_layer": 0.1,
        })
        engine2 = _build_engine(memory, clean)
        graph2 = engine2.run()
        summary2 = build_summary(
            issue_id="i2", branch="t", graph=graph2,
            initial_budget=1_000_000, final_budget=999_000,
            tier2_calls_used=0,
        )
        assert summary2["fix_locality"]["gaming_detected"] is False
        assert summary2["fix_locality"]["gaming_detected_count"] == 0
