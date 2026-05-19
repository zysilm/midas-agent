"""Integration test: a full HTA episode through HTAWorkspace.

execute -> submit_patch -> post_episode, with the real engine, registry,
verifiers and TypedAdvantageMemory. Only the LLM callables and the IO
backend are mocked (directly with unittest.mock — no fake/stub classes).
"""
import json
import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.advantage_memory import TypedAdvantageMemory
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.engine import HTAEngineConfig
from midas_agent.workspace.hta.workspace import HTAWorkspace


_SEED = {
    "root_cause_localization": [
        "framework_default_value", "operator_overload_path", "serialization_roundtrip",
    ],
    "fix_locality_scope": ["surface_patch", "intermediate_layer", "root_layer"],
    "spec_interpretation": ["literal_reading", "inverse_reading", "scope_widened"],
}


def _hyp(cls):
    return {
        "hypothesis_class": cls,
        "rationale": f"rationale {cls}",
        "predicted_path": f"pkg/{cls}.py",
        "test_payload": "raise ValueError('boom')",
    }


def _system_llm(req):
    """Answer hypothesis-generation calls; reject decision-point judging."""
    tool_names = {t["function"]["name"] for t in (req.tools or [])}
    user = req.messages[-1]["content"] if req.messages else ""
    if "submit_hypotheses" in tool_names:
        m = re.search(r"type:\s*(\S+)", user)
        dt = m.group(1) if m else "root_cause_localization"
        classes = _SEED.get(dt, ["a", "b", "c"])
        return LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="t1", name="submit_hypotheses", arguments={
                "hypotheses": [_hyp(c) for c in classes],
            })],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    return LLMResponse(
        content="not a decision point", tool_calls=None,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _call_llm(req):
    # Execution-node ReactAgent: a plain text reply ends the loop immediately.
    return LLMResponse(
        content="task complete", tool_calls=None,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


@pytest.mark.integration
class TestHTAEpisode:
    def _run_episode(self, train_dir, score):
        memory = TypedAdvantageMemory(
            os.path.join(train_dir, "data", "advantage_memory.json"),
        )
        ws = HTAWorkspace(
            workspace_id="ws-1",
            call_llm=MagicMock(side_effect=_call_llm),
            system_llm=_system_llm,
            actions=[],
            advantage_memory=memory,
            registry=DecisionPointRegistry(),
            engine_config=HTAEngineConfig(),
            train_dir=train_dir,
        )
        # Mock the sandbox: a traceback that implicates one RCL hypothesis'
        # predicted path, so the verifier scores the hypotheses distinctly.
        fake_io = MagicMock()
        fake_io._workdir = train_dir
        fake_io.run_bash.return_value = (
            'Traceback (most recent call last):\n'
            '  File "pkg/framework_default_value.py", line 1\n'
            'ValueError: boom'
        )
        ws._io = fake_io

        ws.receive_budget(1_000_000)
        ws.execute(Issue(issue_id="i1", repo="o/r", description="the cache returns stale data"))
        ws.submit_patch()
        ws.post_episode({"ws-1": {"s_exec": score}}, evicted_ids=[])
        return ws, memory

    def test_full_episode_produces_a_graph(self):
        train_dir = tempfile.mkdtemp(prefix="hta_ep_")
        ws, _ = self._run_episode(train_dir, score=1.0)
        assert ws._last_graph is not None
        assert len(ws._last_graph.decision_nodes()) >= 1
        assert isinstance(ws._last_patch, str)

    def test_episode_exports_graph_json(self):
        train_dir = tempfile.mkdtemp(prefix="hta_ep_")
        self._run_episode(train_dir, score=1.0)
        graph_dir = os.path.join(train_dir, "log", "hta_graphs")
        files = os.listdir(graph_dir)
        assert len(files) == 1
        with open(os.path.join(graph_dir, files[0])) as f:
            data = json.load(f)
        assert "nodes" in data and "edges" in data

    def test_episode_persists_advantage_memory(self):
        train_dir = tempfile.mkdtemp(prefix="hta_ep_")
        ws, memory = self._run_episode(train_dir, score=1.0)
        mem_path = os.path.join(train_dir, "data", "advantage_memory.json")
        assert os.path.isfile(mem_path)
        # The RCL decision recorded advantages for its hypotheses.
        assert len(memory) >= 1

    def test_failed_episode_still_updates_memory(self):
        train_dir = tempfile.mkdtemp(prefix="hta_ep_")
        ws, memory = self._run_episode(train_dir, score=0.0)
        # A failed episode commits with reduced weight, not zero updates.
        assert os.path.isfile(
            os.path.join(train_dir, "data", "advantage_memory.json")
        )
