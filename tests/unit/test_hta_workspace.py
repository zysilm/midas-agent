"""Unit tests for HTAWorkspace.

Dependencies are mocked directly with unittest.mock — no fake/stub classes.
The HTAEngine is patched so the workspace's own lifecycle logic is exercised
in isolation.
"""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from llm_agent_toolkit.types import Issue

from midas_agent.workspace.base import Workspace
from midas_agent.workspace.hta.advantage_memory import TypedAdvantageMemory
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.engine import HTAEngineConfig
from midas_agent.workspace.hta.graph import DecisionGraph, NodeKind
from midas_agent.workspace.hta.workspace import HTAWorkspace


@pytest.fixture
def train_dir():
    return tempfile.mkdtemp(prefix="hta_ws_")


def _make_workspace(train_dir, memory=None):
    if memory is None:
        memory = MagicMock(spec=TypedAdvantageMemory)
    return HTAWorkspace(
        workspace_id="ws-1",
        call_llm=MagicMock(),
        system_llm=MagicMock(),
        actions=[],
        advantage_memory=memory,
        registry=DecisionPointRegistry(),
        engine_config=HTAEngineConfig(),
        train_dir=train_dir,
    )


def _issue():
    return Issue(issue_id="i1", repo="o/r", description="a bug")


@pytest.mark.unit
class TestHTAWorkspace:
    def test_is_a_workspace(self):
        assert issubclass(HTAWorkspace, Workspace)

    def test_receive_budget_accumulates(self, train_dir):
        ws = _make_workspace(train_dir)
        ws.receive_budget(1000)
        ws.receive_budget(500)
        assert ws._budget == 1500
        assert ws.budget_received == 1500

    def test_execute_runs_engine_and_stores_graph(self, train_dir):
        ws = _make_workspace(train_dir)
        graph = DecisionGraph()
        graph.add_node(NodeKind.DECISION, "root_cause_localization")
        with patch("midas_agent.workspace.hta.workspace.HTAEngine") as MockEngine:
            MockEngine.return_value.run.return_value = graph
            ws.execute(_issue())
        assert ws._last_graph is graph
        MockEngine.return_value.run.assert_called_once()

    def test_execute_survives_engine_failure(self, train_dir):
        ws = _make_workspace(train_dir)
        with patch("midas_agent.workspace.hta.workspace.HTAEngine") as MockEngine:
            MockEngine.return_value.run.side_effect = RuntimeError("boom")
            ws.execute(_issue())  # must not raise
        assert ws._last_graph is None

    def test_submit_patch_uses_git_diff(self, train_dir):
        ws = _make_workspace(train_dir)
        fake_io = MagicMock()
        fake_io._workdir = "/testbed"
        fake_io.run_bash.return_value = "diff --git a/x b/x\n+fix"
        ws._io = fake_io
        ws.submit_patch()
        assert "diff --git" in ws._last_patch

    def test_submit_patch_survives_io_failure(self, train_dir):
        ws = _make_workspace(train_dir)
        fake_io = MagicMock()
        fake_io._workdir = "/testbed"
        fake_io.run_bash.side_effect = RuntimeError("docker gone")
        ws._io = fake_io
        ws.submit_patch()  # must not raise
        assert ws._last_patch == ""

    def test_post_episode_commits_advantages_on_success(self, train_dir):
        memory = MagicMock(spec=TypedAdvantageMemory)
        ws = _make_workspace(train_dir, memory=memory)
        ws._last_graph = DecisionGraph()
        ws.post_episode({"ws-1": {"s_exec": 1.0}}, evicted_ids=[])
        memory.commit_pending.assert_called_once_with(outcome_score=1.0)
        memory.discard_pending.assert_not_called()

    def test_post_episode_discards_when_no_graph(self, train_dir):
        memory = MagicMock(spec=TypedAdvantageMemory)
        ws = _make_workspace(train_dir, memory=memory)
        ws._last_graph = None
        ws.post_episode({"ws-1": {"s_exec": 0.0}}, evicted_ids=[])
        memory.discard_pending.assert_called_once()
        memory.commit_pending.assert_not_called()

    def test_post_episode_exports_graph_json(self, train_dir):
        ws = _make_workspace(train_dir, memory=MagicMock(spec=TypedAdvantageMemory))
        graph = DecisionGraph()
        graph.add_node(NodeKind.DECISION, "root_cause_localization")
        ws._last_graph = graph
        ws.post_episode({"ws-1": {"s_exec": 1.0}}, evicted_ids=[])
        graph_dir = os.path.join(train_dir, "log", "hta_graphs")
        assert os.path.isdir(graph_dir)
        assert len(os.listdir(graph_dir)) == 1

    def test_post_episode_missing_workspace_score_defaults_zero(self, train_dir):
        memory = MagicMock(spec=TypedAdvantageMemory)
        ws = _make_workspace(train_dir, memory=memory)
        ws._last_graph = DecisionGraph()
        ws.post_episode({}, evicted_ids=[])  # no entry for ws-1
        memory.commit_pending.assert_called_once_with(outcome_score=0.0)
