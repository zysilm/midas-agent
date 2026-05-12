"""Unit tests for DAGExecutor, ExecutionResult, and CyclicDependencyError."""
from unittest.mock import MagicMock

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
from llm_agent_toolkit.stdlib.action import Action, ActionRegistry
from llm_agent_toolkit.stdlib.actions.bash import BashAction
from llm_agent_toolkit.stdlib.actions.str_replace_editor import StrReplaceEditorAction
from llm_agent_toolkit.stdlib.actions.task_done import TaskDoneAction
from llm_agent_toolkit.types import Issue
from llm_agent_toolkit.llm.types import LLMResponse, TokenUsage, ToolCall


def _all_actions() -> list[Action]:
    return [BashAction(), StrReplaceEditorAction(), TaskDoneAction()]


@pytest.mark.unit
class TestExecutionResult:
    """Tests for the ExecutionResult data class."""

    def test_execution_result_fields(self):
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
        return Issue(issue_id="issue-1", repo="test/repo", description="Fix the bug")

    def _make_call_llm(self, content: str = "done"):
        """Scripted call_llm: bash tool call → text response."""
        responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
            LLMResponse(
                content=content,
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
        ]
        idx = {"i": 0}

        def call_llm(request):
            i = idx["i"]
            idx["i"] += 1
            return responses[i] if i < len(responses) else responses[-1]

        return call_llm

    def test_construction(self):
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)
        assert executor is not None

    def test_execute_linear_dag(self):
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        config = WorkflowConfig(
            meta=ConfigMeta(name="linear", description="one step"),
            steps=[StepConfig(id="s1", prompt="do it", tools=["bash"])],
        )
        result = executor.execute(config, self._make_issue(), self._make_call_llm())

        assert isinstance(result, ExecutionResult)
        assert "s1" in result.step_outputs

    def test_execute_multi_step_dag(self):
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        config = WorkflowConfig(
            meta=ConfigMeta(name="multi", description="two steps"),
            steps=[
                StepConfig(id="s1", prompt="analyze"),
                StepConfig(id="s2", prompt="patch", inputs=["s1"]),
            ],
        )
        result = executor.execute(config, self._make_issue(), self._make_call_llm())

        assert isinstance(result, ExecutionResult)
        assert "s1" in result.step_outputs
        assert "s2" in result.step_outputs

    def test_execute_cyclic_dag_raises(self):
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        config = WorkflowConfig(
            meta=ConfigMeta(name="cyclic", description="cycle"),
            steps=[
                StepConfig(id="a", prompt="step a", inputs=["b"]),
                StepConfig(id="b", prompt="step b", inputs=["a"]),
            ],
        )
        with pytest.raises(CyclicDependencyError):
            executor.execute(config, self._make_issue(), self._make_call_llm())

    def test_execute_step_failure_aborts(self):
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        config = WorkflowConfig(
            meta=ConfigMeta(name="fail", description="failure"),
            steps=[StepConfig(id="s1", prompt="will fail", tools=["bash"])],
        )
        call_llm = MagicMock(side_effect=RuntimeError("LLM call failed"))

        result = executor.execute(config, self._make_issue(), call_llm)

        assert result.aborted is True
        assert result.abort_step == "s1"

    def test_execute_injects_prior_outputs(self):
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        config = WorkflowConfig(
            meta=ConfigMeta(name="inject", description="output injection"),
            steps=[
                StepConfig(id="gather", prompt="gather info"),
                StepConfig(id="use", prompt="use gathered info", inputs=["gather"]),
            ],
        )
        result = executor.execute(config, self._make_issue(), self._make_call_llm("gathered data"))

        assert "gather" in result.step_outputs
        assert "use" in result.step_outputs

    def test_all_steps_get_all_tools(self):
        """Every step gets all registered actions regardless of step.tools."""
        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        config = WorkflowConfig(
            meta=ConfigMeta(name="tools", description="tool test"),
            steps=[StepConfig(id="s1", prompt="do it", tools=["bash"])],  # only bash in config
        )

        # Patch ReactAgent to capture what actions it receives
        import midas_agent.workspace.config_evolution.executor as exec_mod
        original_init = exec_mod.ReactAgent.__init__
        captured_actions = []

        def spy_init(self, *args, **kwargs):
            captured_actions.extend(kwargs.get("actions", args[1] if len(args) > 1 else []))
            original_init(self, *args, **kwargs)

        exec_mod.ReactAgent.__init__ = spy_init
        try:
            executor.execute(config, self._make_issue(), self._make_call_llm())
        finally:
            exec_mod.ReactAgent.__init__ = original_init

        action_names = {a.name for a in captured_actions}
        assert "bash" in action_names
        assert "str_replace_editor" in action_names
        assert "task_done" in action_names
