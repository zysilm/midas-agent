"""Unit tests for ConfigEvolutionWorkspace."""
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.config_evolution.workspace import ConfigEvolutionWorkspace
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)
from midas_agent.workspace.config_evolution.config_creator import ConfigCreator, ConfigMerger
from midas_agent.workspace.config_evolution.executor import DAGExecutor, ExecutionResult
from midas_agent.workspace.config_evolution.prompt_optimizer import GEPAConfigOptimizer
from midas_agent.workspace.config_evolution.snapshot_store import ConfigSnapshotStore
from midas_agent.evaluation.module import EvalResult
from midas_agent.types import Issue
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


@pytest.mark.unit
class TestConfigEvolutionWorkspace:
    """Tests for the ConfigEvolutionWorkspace class."""

    def _make_call_llm(self):
        """Create a fake call_llm callback."""
        return MagicMock(
            return_value=LLMResponse(
                content="response",
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        )

    def _make_workflow_config(self) -> WorkflowConfig:
        """Create a simple WorkflowConfig for testing."""
        meta = ConfigMeta(name="test-wf", description="test workflow")
        step = StepConfig(id="s1", prompt="do something")
        return WorkflowConfig(meta=meta, steps=[step])

    def _make_workspace(self) -> ConfigEvolutionWorkspace:
        """Create a ConfigEvolutionWorkspace with mocked dependencies."""
        wc = self._make_workflow_config()
        optimizer = MagicMock(spec=GEPAConfigOptimizer)
        # maybe_optimize must return a real WorkflowConfig, not a Mock
        optimizer.maybe_optimize.return_value = (wc, False)
        merger = MagicMock(spec=ConfigMerger)
        merger.merge.return_value = wc
        return ConfigEvolutionWorkspace(
            workspace_id="ws-1",
            workflow_config=wc,
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            dag_executor=MagicMock(spec=DAGExecutor),
            prompt_optimizer=optimizer,
            config_creator=MagicMock(spec=ConfigCreator),
            config_merger=merger,
            snapshot_store=MagicMock(spec=ConfigSnapshotStore),
        )

    def test_is_workspace_subclass(self):
        """ConfigEvolutionWorkspace is a subclass of Workspace."""
        assert issubclass(ConfigEvolutionWorkspace, Workspace)

    def test_construction(self):
        """ConfigEvolutionWorkspace can be constructed with all required arguments."""
        ws = self._make_workspace()

        assert ws is not None

    def test_receive_budget(self):
        """receive_budget() accepts a token budget amount."""
        ws = self._make_workspace()

        ws.receive_budget(1000)  # Should not raise

    def test_execute_delegates_to_dag_executor(self):
        """execute() delegates to the DAGExecutor to run the workflow."""
        wc = self._make_workflow_config()
        dag_executor = MagicMock(spec=DAGExecutor)
        dag_executor.execute.return_value = ExecutionResult(
            step_outputs={"s1": "done"},
            patch="diff...",
            aborted=False,
            abort_step=None,
        )
        optimizer = MagicMock(spec=GEPAConfigOptimizer)
        optimizer.maybe_optimize.return_value = (wc, False)
        merger = MagicMock(spec=ConfigMerger)
        merger.merge.return_value = wc
        ws = ConfigEvolutionWorkspace(
            workspace_id="ws-1",
            workflow_config=wc,
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            dag_executor=dag_executor,
            prompt_optimizer=optimizer,
            config_creator=MagicMock(spec=ConfigCreator),
            config_merger=merger,
            snapshot_store=MagicMock(spec=ConfigSnapshotStore),
        )
        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix the bug",
        )

        ws.execute(issue)

        dag_executor.execute.assert_called_once()

    def test_submit_patch(self):
        """submit_patch() sets _last_patch on the workspace."""
        ws = self._make_workspace()

        ws.submit_patch()  # Should not raise
        assert hasattr(ws, "_last_patch")

    def test_post_episode_evicted_returns_none(self):
        """An evicted workspace returns None from post_episode().

        Evicted workspaces no longer produce their own replacement config.
        The scheduler seeds replacements with the best-eta config.
        """
        ws = self._make_workspace()

        eval_results = {"ws-1": {"s_w": 0.36, "s_exec": 0.3}}
        result = ws.post_episode(eval_results, evicted_ids=["ws-1"])

        assert result is None

    def test_post_episode_survivor_returns_none(self):
        """A surviving workspace returns None from post_episode().

        When workspace_id is not in evicted_ids, post_episode triggers
        GEPAConfigOptimizer.maybe_optimize() and returns None.
        """
        ws = self._make_workspace()

        eval_results = {"ws-1": {"s_w": 0.9, "s_exec": 0.9}}
        result = ws.post_episode(eval_results, evicted_ids=[])

        assert result is None
