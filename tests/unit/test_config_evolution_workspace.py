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
from midas_agent.workspace.config_evolution.executor import DAGExecutor, ExecutionResult
from midas_agent.workspace.config_evolution.mutator import ConfigMutator
from midas_agent.workspace.config_evolution.snapshot_store import ConfigSnapshotStore
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
        return ConfigEvolutionWorkspace(
            workspace_id="ws-1",
            workflow_config=self._make_workflow_config(),
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            dag_executor=MagicMock(spec=DAGExecutor),
            config_mutator=MagicMock(spec=ConfigMutator),
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
        dag_executor = MagicMock(spec=DAGExecutor)
        dag_executor.execute.return_value = ExecutionResult(
            step_outputs={"s1": "done"},
            patch="diff...",
            aborted=False,
            abort_step=None,
        )
        ws = ConfigEvolutionWorkspace(
            workspace_id="ws-1",
            workflow_config=self._make_workflow_config(),
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            dag_executor=dag_executor,
            config_mutator=MagicMock(spec=ConfigMutator),
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
        """submit_patch() persists the generated patch."""
        ws = self._make_workspace()

        ws.submit_patch()  # Should not raise

    def test_post_episode_evicted_returns_new_config(self):
        """An evicted workspace returns a new config dict from post_episode()."""
        ws = self._make_workspace()

        result = ws.post_episode({"evicted": True, "score": 0.3, "cost": 200})

        assert isinstance(result, dict)

    def test_post_episode_survivor_returns_none(self):
        """A surviving workspace returns None from post_episode()."""
        ws = self._make_workspace()

        result = ws.post_episode({"evicted": False, "score": 0.9, "cost": 100})

        assert result is None
