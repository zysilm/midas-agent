"""Tests for spec_interpretation re-entry counter (issue H1 D3).

The boolean self._escalated latch was replaced with a bounded counter
self._escalation_count so RCL can re-escalate up to max_escalations times
per issue, and the cap is configurable. The counter resets in run() so
the cap is per-issue.

Tests exercise the counter via direct attribute access on a constructed
engine (the cleanest way to assert state transitions; the alternative
would be an end-to-end run sequence which is fragile to backbone changes).
"""
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.advantage_memory import SemanticExperienceMemory
from midas_agent.workspace.hta.decision_point import DecisionPoint, DecisionPointRegistry, Hypothesis
from midas_agent.workspace.hta.engine import HTAEngine, HTAEngineConfig, _DecisionResult
from midas_agent.workspace.hta.graph import DecisionGraph, NodeKind, NodeStatus


def _text_response():
    return LLMResponse(
        content="done", tool_calls=None,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _build_engine(config=None):
    issue = Issue(issue_id="i1", repo="o/r", description="bug")
    d = tempfile.mkdtemp(prefix="hta_esc_")
    memory = SemanticExperienceMemory(os.path.join(d, "mem.json"))
    engine = HTAEngine(
        issue=issue,
        call_llm=MagicMock(return_value=_text_response()),
        system_llm=MagicMock(return_value=_text_response()),
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


def _bootstrap_graph_and_node(engine):
    graph = DecisionGraph()
    root = graph.add_node(NodeKind.EXECUTION, "bootstrap", status=NodeStatus.DONE)
    node = graph.add_node(
        NodeKind.DECISION, "root_cause_localization",
        decision_type="root_cause_localization", status=NodeStatus.RUNNING,
    )
    graph.add_edge(root.node_id, node.node_id, reason="advance")
    return graph, node


def _patch_resolve_to_escalate(engine):
    """Replace engine._resolve so it always returns an escalation result."""
    def fake_resolve(dp, graph, node_id, action_history, stuck_reason=None):
        return _DecisionResult(hypotheses=[], escalated=True)
    engine._resolve = fake_resolve


@pytest.mark.unit
class TestEscalationCounter:
    def test_first_collapse_escalates(self):
        # escalation_count starts at 0; after one std=0 RCL, count becomes 1.
        engine = _build_engine()
        assert engine._escalation_count == 0
        _patch_resolve_to_escalate(engine)
        graph, _ = _bootstrap_graph_and_node(engine)
        from collections import deque
        from midas_agent.workspace.hta.engine import _Step
        worklist: deque[_Step] = deque()
        engine._run_decision_step(
            _Step("decision", decision_type="root_cause_localization"),
            graph, list(graph.nodes.values())[0].node_id, worklist, [],
        )
        assert engine._escalation_count == 1
        # spec_interpretation step was prepended to the worklist.
        assert any(s.decision_type == "spec_interpretation" for s in worklist)

    def test_second_collapse_escalates_again(self):
        # escalation_count=1 going in: another collapse advances to 2.
        engine = _build_engine()
        engine._escalation_count = 1
        _patch_resolve_to_escalate(engine)
        graph, _ = _bootstrap_graph_and_node(engine)
        from collections import deque
        from midas_agent.workspace.hta.engine import _Step
        worklist: deque[_Step] = deque()
        engine._run_decision_step(
            _Step("decision", decision_type="root_cause_localization"),
            graph, list(graph.nodes.values())[0].node_id, worklist, [],
        )
        assert engine._escalation_count == 2
        assert any(s.decision_type == "spec_interpretation" for s in worklist)

    def test_third_collapse_within_cap(self):
        engine = _build_engine(HTAEngineConfig(max_escalations=3))
        engine._escalation_count = 2
        _patch_resolve_to_escalate(engine)
        graph, _ = _bootstrap_graph_and_node(engine)
        from collections import deque
        from midas_agent.workspace.hta.engine import _Step
        worklist: deque[_Step] = deque()
        engine._run_decision_step(
            _Step("decision", decision_type="root_cause_localization"),
            graph, list(graph.nodes.values())[0].node_id, worklist, [],
        )
        assert engine._escalation_count == 3
        assert any(s.decision_type == "spec_interpretation" for s in worklist)

    def test_fourth_collapse_falls_through_to_raw_score(self):
        # cap=3, count already at 3 → escalation suppressed, fall through.
        engine = _build_engine(HTAEngineConfig(max_escalations=3))
        engine._escalation_count = 3
        _patch_resolve_to_escalate(engine)
        graph, _ = _bootstrap_graph_and_node(engine)
        from collections import deque
        from midas_agent.workspace.hta.engine import _Step
        worklist: deque[_Step] = deque()
        engine._run_decision_step(
            _Step("decision", decision_type="root_cause_localization"),
            graph, list(graph.nodes.values())[0].node_id, worklist, [],
        )
        # Count not incremented; spec_interpretation NOT added to worklist.
        assert engine._escalation_count == 3
        assert not any(s.decision_type == "spec_interpretation" for s in worklist)

    def test_counter_resets_between_issues(self):
        # The reset happens in run(); simulate by running twice.
        engine = _build_engine()
        # Force escalation on first run.
        _patch_resolve_to_escalate(engine)
        engine.run()
        # First run hit at least one escalation.
        first_run_count = engine._escalation_count
        # Reset and re-run: the counter starts at 0 again for the second issue.
        # We can't easily run twice without a fresh setup; check that run()
        # zeroes the counter even if it was non-zero before.
        engine._escalation_count = 99
        # Replace _resolve again to avoid actually escalating this time —
        # we just want to observe the reset behaviour.
        def no_escalate(dp, graph, node_id, action_history, stuck_reason=None):
            return _DecisionResult(failed=True)
        engine._resolve = no_escalate
        engine.run()
        # After run(), the counter should be 0 (no escalations this run).
        assert engine._escalation_count == 0

    def test_max_escalations_configurable(self):
        engine = _build_engine(HTAEngineConfig(max_escalations=1))
        _patch_resolve_to_escalate(engine)
        graph, _ = _bootstrap_graph_and_node(engine)
        from collections import deque
        from midas_agent.workspace.hta.engine import _Step
        worklist: deque[_Step] = deque()
        # First call: under cap (1), escalates.
        engine._run_decision_step(
            _Step("decision", decision_type="root_cause_localization"),
            graph, list(graph.nodes.values())[0].node_id, worklist, [],
        )
        assert engine._escalation_count == 1
        # Second call: at cap, suppressed.
        worklist2: deque[_Step] = deque()
        engine._run_decision_step(
            _Step("decision", decision_type="root_cause_localization"),
            graph, list(graph.nodes.values())[0].node_id, worklist2, [],
        )
        assert engine._escalation_count == 1
        assert not any(s.decision_type == "spec_interpretation" for s in worklist2)
