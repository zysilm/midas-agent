"""Integration tests for HTAEngine.

All collaborators are mocked directly with unittest.mock — no fake/stub
classes. call_llm/system_llm are MagicMocks returning real LLMResponse
objects; the engine's sub-verifiers are replaced with MagicMock(spec=...)
so verifier scores are fully controlled.
"""
import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.advantage_memory import TypedAdvantageMemory
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.engine import HTAEngine, HTAEngineConfig
from midas_agent.workspace.hta.graph import NodeKind
from midas_agent.workspace.hta.sub_verifier import SubVerifier


# --- LLM response builders --------------------------------------------------

_SEED_CLASSES = {
    "root_cause_localization": [
        "framework_default_value", "operator_overload_path", "serialization_roundtrip",
    ],
    "fix_locality_scope": ["surface_patch", "intermediate_layer", "root_layer"],
    "spec_interpretation": ["literal_reading", "inverse_reading", "scope_widened"],
    "investigation_continuation": ["persist_same_path", "pivot_target", "abandon"],
}


def _hyp_dict(cls):
    return {
        "hypothesis_class": cls,
        "rationale": f"rationale for {cls}",
        "predicted_path": f"pkg/{cls}.py",
        "test_payload": "print('probe')",
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


def _text_response(text="done"):
    return LLMResponse(
        content=text, tool_calls=None,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _make_system_llm(decision_point_verdict=False):
    """Return a system_llm callable that answers hypothesis-gen and meta-judge calls."""
    def system_llm(req):
        tool_names = {t["function"]["name"] for t in (req.tools or [])}
        user = req.messages[-1]["content"] if req.messages else ""
        if "submit_hypotheses" in tool_names:
            m = re.search(r"type:\s*(\S+)", user)
            dt = m.group(1) if m else "root_cause_localization"
            classes = _SEED_CLASSES.get(dt, ["a", "b", "c"])
            return _hyp_response(classes)
        if "judge_decision_point" in tool_names:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="j1", name="judge_decision_point", arguments={
                    "is_decision_point": decision_point_verdict,
                    "decision_type": "investigation_continuation",
                    "path_dependency": True,
                    "enumerable_alternatives": True,
                    "delayed_verification": True,
                    "reason": "test",
                })],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        return _text_response()
    return system_llm


def _scored_verifier(score_map, default=0.5):
    """A MagicMock(spec=SubVerifier) whose verify() scores by hypothesis name."""
    v = MagicMock(spec=SubVerifier)
    v.verify.side_effect = lambda h, ctx, cheap=True: score_map.get(h.name, default)
    return v


def _build_engine(memory, system_llm, verifiers=None, config=None):
    issue = Issue(issue_id="i1", repo="o/r", description="the widget crashes on save")
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
    if verifiers is not None:
        engine._verifiers.update(verifiers)
    return engine


@pytest.fixture
def memory():
    d = tempfile.mkdtemp(prefix="hta_eng_")
    return TypedAdvantageMemory(os.path.join(d, "mem.json"))


@pytest.mark.integration
class TestHTAEngineRun:
    def test_backbone_runs_to_completion(self, memory):
        # Distinct verifier scores -> no advantage collapse -> no escalation.
        distinct = _scored_verifier({
            "framework_default_value": 0.9, "operator_overload_path": 0.5,
            "serialization_roundtrip": 0.1,
            "surface_patch": 0.8, "intermediate_layer": 0.4, "root_layer": 0.2,
        })
        engine = _build_engine(
            memory, _make_system_llm(),
            verifiers={
                "root_cause_localization": distinct,
                "fix_locality_scope": distinct,
            },
        )
        graph = engine.run()
        # root + 2 decisions + 4 execution nodes.
        assert len(graph.decision_nodes()) == 2
        kinds = [n.kind for n in graph.nodes.values()]
        assert kinds.count(NodeKind.EXECUTION) == 5  # bootstrap + 4 backbone
        assert "root_cause_localization" in {
            n.decision_type for n in graph.decision_nodes()
        }

    def test_winner_is_highest_advantage(self, memory):
        verifier = _scored_verifier({
            "framework_default_value": 1.0,
            "operator_overload_path": 0.2,
            "serialization_roundtrip": 0.0,
        })
        engine = _build_engine(
            memory, _make_system_llm(),
            verifiers={"root_cause_localization": verifier},
        )
        graph = engine.run()
        rcl = next(
            n for n in graph.decision_nodes()
            if n.decision_type == "root_cause_localization"
        )
        assert rcl.winner_hypothesis == "framework_default_value"

    def test_all_advantages_buffered_to_memory(self, memory):
        verifier = _scored_verifier({
            "framework_default_value": 1.0,
            "operator_overload_path": 0.5,
            "serialization_roundtrip": 0.0,
        })
        engine = _build_engine(
            memory, _make_system_llm(),
            verifiers={"root_cause_localization": verifier},
        )
        engine.run()
        # Winner AND losers are buffered (pending until commit_pending).
        rcl_pending = [p for p in memory._pending if p[0] == "root_cause_localization"]
        assert len(rcl_pending) == 3
        # Advantages within a group are centred on zero.
        advs = [p[2] for p in rcl_pending]
        assert sum(advs) == pytest.approx(0.0, abs=1e-9)

    def test_advantage_collapse_triggers_escalation(self, memory):
        # All RCL hypotheses score identically -> std == 0 -> escalate.
        verifier = _scored_verifier({}, default=0.5)
        engine = _build_engine(
            memory, _make_system_llm(),
            verifiers={"root_cause_localization": verifier},
        )
        graph = engine.run()
        dtypes = [n.decision_type for n in graph.decision_nodes()]
        # Escalation splices in spec_interpretation and re-enters RCL.
        assert "spec_interpretation" in dtypes
        assert dtypes.count("root_cause_localization") == 2
        # The re-entry RCL node has a backward edge.
        assert any(e.kind == "backward" for e in graph.edges)

    def test_escalation_actually_runs_spec_interpretation_race(self, memory):
        """B7 verification: after dropping adaptive_g, the escalation path
        is not just reachable — the spec_interpretation decision actually
        runs its G=3 hypothesis race and writes advantages to memory.
        Previously this was structurally unreachable (issue #44 B7).
        """
        # Force RCL collapse, but give spec_interpretation distinct scores
        # so its race produces real advantages.
        rcl_verifier = _scored_verifier({}, default=0.5)
        spec_verifier = _scored_verifier({
            "literal_reading": 0.9, "inverse_reading": 0.4, "scope_widened": 0.1,
        })
        engine = _build_engine(
            memory, _make_system_llm(),
            verifiers={
                "root_cause_localization": rcl_verifier,
                "spec_interpretation": spec_verifier,
            },
        )
        engine.run()
        spec_pending = [p for p in memory._pending if p[0] == "spec_interpretation"]
        # All three spec_interpretation hypotheses' advantages buffered.
        assert len(spec_pending) == 3
        # Spec_interpretation race produced real (non-zero) advantages
        # because its verifier returned distinct scores.
        advs = [p[2] for p in spec_pending]
        assert any(abs(a) > 1e-6 for a in advs)

    def test_budget_brake_stops_the_run(self, memory):
        engine = _build_engine(memory, _make_system_llm())
        engine._balance_provider = lambda: 0  # no budget at all
        graph = engine.run()
        # Only the bootstrap node — the loop breaks before any step.
        assert len(graph.decision_nodes()) == 0

    def test_decision_point_cap_is_respected(self, memory):
        engine = _build_engine(
            memory, _make_system_llm(),
            config=HTAEngineConfig(max_decision_points=1),
        )
        graph = engine.run()
        assert len(graph.decision_nodes()) <= 1

    def test_novel_hypothesis_routes_to_tier2_judge(self, memory):
        """A __novel__ hypothesis must be verified by the LLM judge, not by
        the decision point's default verifier — that's the novel-class
        exception (issue #44 C2).
        """
        from midas_agent.workspace.hta.decision_point import Hypothesis

        # Default verifier whose verify() we can assert was NOT called for the novel.
        default_verifier = _scored_verifier({}, default=0.4)
        engine = _build_engine(
            memory, _make_system_llm(),
            verifiers={"root_cause_localization": default_verifier},
        )
        novel = Hypothesis(name="__novel__:fresh_thing", rationale="r")
        seed = Hypothesis(name="framework_default_value", rationale="r")
        picked = engine._select_verifier_for(novel, default_verifier)
        assert picked is engine._novel_class_judge
        picked2 = engine._select_verifier_for(seed, default_verifier)
        assert picked2 is default_verifier

    def test_tier2_cap_is_enforced_per_issue(self, memory):
        from midas_agent.workspace.hta.decision_point import Hypothesis

        engine = _build_engine(memory, _make_system_llm())
        default = _scored_verifier({}, default=0.4)
        novels = [Hypothesis(name=f"__novel__:n{i}", rationale="r") for i in range(5)]
        picked = [engine._select_verifier_for(n, default) for n in novels]
        # First 3 route to the judge; the 4th and 5th fall back to default.
        assert picked[:3] == [engine._novel_class_judge] * 3
        assert picked[3:] == [default, default]
        # And a fresh run() resets the counter.
        engine._tier2_calls_used = 0
        assert engine._select_verifier_for(novels[0], default) is engine._novel_class_judge

    def test_graph_is_json_serialisable(self, memory):
        engine = _build_engine(memory, _make_system_llm())
        graph = engine.run()
        d = graph.to_dict()
        assert "nodes" in d and "edges" in d
        import json
        json.dumps(d)  # must not raise
