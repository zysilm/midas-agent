"""Unit tests for the HTA DecisionGraph."""
import pytest

from midas_agent.workspace.hta.graph import (
    DecisionGraph,
    NodeKind,
    NodeStatus,
)


@pytest.mark.unit
class TestDecisionGraph:
    def test_add_node_assigns_unique_ids(self):
        g = DecisionGraph()
        a = g.add_node(NodeKind.EXECUTION, "a")
        b = g.add_node(NodeKind.DECISION, "b", decision_type="root_cause_localization")
        assert a.node_id != b.node_id
        assert g.nodes[a.node_id] is a
        assert b.decision_type == "root_cause_localization"

    def test_add_edge_and_predecessors(self):
        g = DecisionGraph()
        a = g.add_node(NodeKind.EXECUTION, "a")
        b = g.add_node(NodeKind.EXECUTION, "b")
        g.add_edge(a.node_id, b.node_id)
        preds = g.predecessors(b.node_id)
        assert [n.node_id for n in preds] == [a.node_id]

    def test_predecessors_filter_by_edge_kind(self):
        g = DecisionGraph()
        a = g.add_node(NodeKind.EXECUTION, "a")
        b = g.add_node(NodeKind.EXECUTION, "b")
        c = g.add_node(NodeKind.DECISION, "c")
        g.add_edge(a.node_id, c.node_id, kind="forward")
        g.add_edge(b.node_id, c.node_id, kind="backward")
        assert {n.node_id for n in g.predecessors(c.node_id, kind="forward")} == {a.node_id}
        assert {n.node_id for n in g.predecessors(c.node_id, kind="backward")} == {b.node_id}

    def test_trace_evidence_concatenates_forward_ancestors(self):
        g = DecisionGraph()
        a = g.add_node(NodeKind.EXECUTION, "localize")
        a.distilled_evidence = "bug is in parser"
        b = g.add_node(NodeKind.EXECUTION, "reproduce")
        b.distilled_evidence = "repro confirms crash"
        c = g.add_node(NodeKind.EXECUTION, "fix")
        g.add_edge(a.node_id, b.node_id)
        g.add_edge(b.node_id, c.node_id)
        trace = g.trace_evidence(c.node_id)
        assert "bug is in parser" in trace
        assert "repro confirms crash" in trace
        # The node itself is not included in its own evidence trace.
        assert trace.index("bug is in parser") < trace.index("repro confirms crash")

    def test_trace_evidence_skips_backward_edges(self):
        """A backward (re-entry) edge must not pull evidence into a loop."""
        g = DecisionGraph()
        a = g.add_node(NodeKind.EXECUTION, "a")
        a.distilled_evidence = "evidence A"
        b = g.add_node(NodeKind.DECISION, "b")
        g.add_edge(a.node_id, b.node_id, kind="forward")
        # Backward edge from b to a — must not cause infinite recursion.
        g.add_edge(b.node_id, a.node_id, kind="backward")
        trace = g.trace_evidence(b.node_id)
        assert "evidence A" in trace

    def test_decision_nodes(self):
        g = DecisionGraph()
        g.add_node(NodeKind.EXECUTION, "e")
        g.add_node(NodeKind.DECISION, "d1")
        g.add_node(NodeKind.DECISION, "d2")
        assert len(g.decision_nodes()) == 2

    def test_to_dict_round_trips_structure(self):
        g = DecisionGraph()
        a = g.add_node(NodeKind.EXECUTION, "a", status=NodeStatus.DONE)
        b = g.add_node(NodeKind.DECISION, "b", decision_type="fix_locality_scope")
        g.add_edge(a.node_id, b.node_id, reason="advance")
        d = g.to_dict()
        assert len(d["nodes"]) == 2
        assert len(d["edges"]) == 1
        assert d["edges"][0]["reason"] == "advance"
        assert {n["kind"] for n in d["nodes"]} == {"execution", "decision"}
