"""Unit tests for DAGExecutor, ExecutionResult, and CyclicDependencyError."""
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.workspace.config_evolution.executor import (
    CyclicDependencyError,
    DAGExecutor,
    ExecutionResult,
)
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)
from midas_agent.stdlib.action import ActionRegistry
from midas_agent.types import Issue
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


@pytest.mark.unit
class TestExecutionResult:
    """Tests for the ExecutionResult data class."""

    def test_execution_result_fields(self):
        """ExecutionResult stores step_outputs, patch, aborted, and abort_step."""
        result = ExecutionResult(
            step_outputs={"s1": "output1"},
            patch="diff --git ...",
            aborted=False,
            abort_step=None,
        )

        assert result.step_outputs == {"s1": "output1"}
        assert result.patch == "diff --git ..."
        assert result.aborted is False
        assert result.abort_step is None


@pytest.mark.unit
class TestDAGExecutor:
    """Tests for the DAGExecutor class."""

    def _make_issue(self) -> Issue:
        """Create a simple test issue."""
        return Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix the bug",
        )

    def _make_call_llm(self, content: str = "done"):
        """Create a fake call_llm callback."""
        return MagicMock(
            return_value=LLMResponse(
                content=content,
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        )

    def test_construction(self):
        """DAGExecutor can be constructed with an ActionRegistry."""
        registry = MagicMock(spec=ActionRegistry)
        executor = DAGExecutor(action_registry=registry)

        assert executor is not None

    def test_execute_linear_dag(self):
        """Executing a single-step DAG calls ReactAgent and returns an ExecutionResult."""
        registry = MagicMock(spec=ActionRegistry)
        executor = DAGExecutor(action_registry=registry)

        meta = ConfigMeta(name="linear", description="one step")
        config = WorkflowConfig(
            meta=meta,
            steps=[StepConfig(id="s1", prompt="do it", tools=["bash"])],
        )
        issue = self._make_issue()
        call_llm = self._make_call_llm()

        result = executor.execute(config, issue, call_llm)

        assert isinstance(result, ExecutionResult)
        assert "s1" in result.step_outputs

    def test_execute_multi_step_dag(self):
        """Executing a two-step DAG where step 2 depends on step 1."""
        registry = MagicMock(spec=ActionRegistry)
        executor = DAGExecutor(action_registry=registry)

        meta = ConfigMeta(name="multi", description="two steps")
        step1 = StepConfig(id="s1", prompt="analyze")
        step2 = StepConfig(id="s2", prompt="patch", inputs=["s1"])
        config = WorkflowConfig(meta=meta, steps=[step1, step2])
        issue = self._make_issue()
        call_llm = self._make_call_llm()

        result = executor.execute(config, issue, call_llm)

        assert isinstance(result, ExecutionResult)
        assert "s1" in result.step_outputs
        assert "s2" in result.step_outputs

    def test_execute_cyclic_dag_raises(self):
        """Steps with circular dependencies raise CyclicDependencyError."""
        registry = MagicMock(spec=ActionRegistry)
        executor = DAGExecutor(action_registry=registry)

        meta = ConfigMeta(name="cyclic", description="cycle")
        step_a = StepConfig(id="a", prompt="step a", inputs=["b"])
        step_b = StepConfig(id="b", prompt="step b", inputs=["a"])
        config = WorkflowConfig(meta=meta, steps=[step_a, step_b])
        issue = self._make_issue()
        call_llm = self._make_call_llm()

        with pytest.raises(CyclicDependencyError):
            executor.execute(config, issue, call_llm)

    def test_execute_step_failure_aborts(self):
        """When a step fails, the result has aborted=True and abort_step set."""
        registry = MagicMock(spec=ActionRegistry)
        executor = DAGExecutor(action_registry=registry)

        meta = ConfigMeta(name="fail", description="failure scenario")
        config = WorkflowConfig(
            meta=meta,
            steps=[StepConfig(id="s1", prompt="will fail", tools=["bash"])],
        )
        issue = self._make_issue()
        call_llm = MagicMock(side_effect=RuntimeError("LLM call failed"))

        result = executor.execute(config, issue, call_llm)

        assert isinstance(result, ExecutionResult)
        assert result.aborted is True
        assert result.abort_step == "s1"

    def test_execute_injects_prior_outputs(self):
        """Prior step outputs are injected as context into subsequent steps."""
        registry = MagicMock(spec=ActionRegistry)
        executor = DAGExecutor(action_registry=registry)

        meta = ConfigMeta(name="inject", description="output injection")
        step1 = StepConfig(id="gather", prompt="gather info")
        step2 = StepConfig(id="use", prompt="use gathered info", inputs=["gather"])
        config = WorkflowConfig(meta=meta, steps=[step1, step2])
        issue = self._make_issue()

        call_llm = self._make_call_llm("gathered data")
        result = executor.execute(config, issue, call_llm)

        assert isinstance(result, ExecutionResult)
        # The second step should have received the first step's output
        assert "gather" in result.step_outputs
        assert "use" in result.step_outputs
