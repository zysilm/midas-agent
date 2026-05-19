"""DecisionGraph — the outer layer of HTA.

A runtime-constructed graph, NOT a DAG: it grows incrementally as the agent
hits decision points, and it may contain backward edges (a decision point can
be re-entered after a downstream failure). The engine never topologically sorts
it — it walks the graph with an explicit cursor — so backward edges are pure
provenance and never cause non-termination.

Two node kinds: DECISION nodes (where G hypotheses are raced) and EXECUTION
nodes (where a plain ReAct loop runs). Each node carries the distilled evidence
that flows downstream; losing hypotheses contribute nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NodeKind(str, Enum):
    DECISION = "decision"
    EXECUTION = "execution"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ABANDONED = "abandoned"


@dataclass
class GraphNode:
    node_id: str
    kind: NodeKind
    label: str
    status: NodeStatus = NodeStatus.PENDING
    decision_type: str | None = None        # set for DECISION nodes
    winner_hypothesis: str | None = None     # winning hypothesis_class
    distilled_evidence: str = ""             # what flows downstream
    payload: dict = field(default_factory=dict)  # raw hypotheses, advantages, ...

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "label": self.label,
            "status": self.status.value,
            "decision_type": self.decision_type,
            "winner_hypothesis": self.winner_hypothesis,
            "distilled_evidence": self.distilled_evidence,
            "payload": self.payload,
        }


@dataclass
class GraphEdge:
    src: str
    dst: str
    kind: str = "forward"   # "forward" | "backward"
    reason: str = ""

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "kind": self.kind, "reason": self.reason}


class DecisionGraph:
    """An incrementally built decision graph with forward and backward edges."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self._counter = 0

    def add_node(self, kind: NodeKind, label: str, **kwargs) -> GraphNode:
        node_id = f"n{self._counter}"
        self._counter += 1
        node = GraphNode(node_id=node_id, kind=kind, label=label, **kwargs)
        self.nodes[node_id] = node
        return node

    def add_edge(self, src: str, dst: str, kind: str = "forward", reason: str = "") -> GraphEdge:
        edge = GraphEdge(src=src, dst=dst, kind=kind, reason=reason)
        self.edges.append(edge)
        return edge

    def predecessors(self, node_id: str, kind: str | None = None) -> list[GraphNode]:
        """Direct predecessors of a node. ``kind`` filters by edge kind."""
        return [
            self.nodes[e.src]
            for e in self.edges
            if e.dst == node_id and (kind is None or e.kind == kind)
            and e.src in self.nodes
        ]

    def trace_evidence(self, node_id: str) -> str:
        """Concatenate distilled evidence of all forward ancestors of a node.

        Walks forward edges only — backward (re-entry) edges are skipped so the
        accumulated context does not loop. Returns evidence oldest-first.
        """
        seen: set[str] = set()
        ordered: list[GraphNode] = []

        def visit(nid: str) -> None:
            if nid in seen:
                return
            seen.add(nid)
            for pred in self.predecessors(nid, kind="forward"):
                visit(pred.node_id)
            if nid in self.nodes:
                ordered.append(self.nodes[nid])

        visit(node_id)
        parts = [
            f"[{n.label}] {n.distilled_evidence}"
            for n in ordered
            if n.distilled_evidence and n.node_id != node_id
        ]
        return "\n\n".join(parts)

    def decision_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes.values() if n.kind == NodeKind.DECISION]

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }
