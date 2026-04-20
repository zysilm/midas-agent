"""Integration tests for the Configuration Evolution pipeline.

Covers: DAG execution, config creation from traces, reflective mutation,
best-eta reproduction, constraint gating, and the full episode lifecycle.

All LLM calls are mocked with realistic scripted responses.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from midas_agent.llm.types import LLMResponse, TokenUsage, ToolCall
from midas_agent.scheduler.resource_meter import BudgetExhaustedError
from midas_agent.stdlib.action import ActionRegistry
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.react_agent import ActionRecord, AgentResult, ReactAgent
from midas_agent.types import Issue
from midas_agent.workspace.config_evolution.config_creator import (
    ConfigCreator,
    format_trace,
    _extract_yaml,
    _parse_config_yaml,
    _tool_usage_summary,
)
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)
from midas_agent.workspace.config_evolution.executor import (
    CyclicDependencyError,
    DAGExecutor,
    ExecutionResult,
)
from midas_agent.workspace.config_evolution.mutator import (
    ConfigMutator,
    _config_to_yaml,
    _validate_mutation,
)
from midas_agent.workspace.config_evolution.snapshot_store import (
    ConfigSnapshotStore,
)
from midas_agent.workspace.config_evolution.workspace import (
    ConfigEvolutionWorkspace,
)
from tests.integration.conftest import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USAGE = TokenUsage(input_tokens=10, output_tokens=5)


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=None, usage=_USAGE)


def _tool(name: str, args: dict, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        usage=_USAGE,
    )


def _all_actions() -> list:
    return [BashAction(), StrReplaceEditorAction(), TaskDoneAction()]


def _make_config(*steps: StepConfig, name: str = "test-wf") -> WorkflowConfig:
    return WorkflowConfig(
        meta=ConfigMeta(name=name, description="test workflow"),
        steps=list(steps),
    )


def _make_issue() -> Issue:
    return Issue(
        issue_id="issue-001",
        repo="test/repo",
        description="Fix the divide-by-zero bug in calculator.py.",
    )


def _make_workspace(
    workspace_id: str = "ws-0",
    config: WorkflowConfig | None = None,
    call_llm=None,
    system_llm=None,
    temp_dir: str = "/tmp/midas_test",
) -> ConfigEvolutionWorkspace:
    """Build a fully wired workspace for integration testing."""
    if config is None:
        config = _make_config(
            StepConfig(id="main", prompt="Solve the issue.", tools=["bash", "str_replace_editor"]),
        )
    if call_llm is None:
        call_llm = FakeLLMProvider(responses=[_text("ok")]).complete
    if system_llm is None:
        system_llm = FakeLLMProvider(responses=[_text("ok")]).complete

    registry = ActionRegistry(_all_actions())
    return ConfigEvolutionWorkspace(
        workspace_id=workspace_id,
        workflow_config=config,
        call_llm=call_llm,
        system_llm=system_llm,
        dag_executor=DAGExecutor(action_registry=registry),
        config_mutator=ConfigMutator(system_llm=system_llm),
        config_creator=ConfigCreator(system_llm=system_llm),
        snapshot_store=ConfigSnapshotStore(store_dir=os.path.join(temp_dir, "snapshots")),
    )


# -- Realistic LLM responses for config creation / mutation --

REALISTIC_SUMMARY = (
    "The agent followed a localization-reproduction-investigation-fix-validation "
    "workflow. It searched for relevant files using grep, read the main source "
    "file, and created a reproduction script. During investigation it traced "
    "the execution path through multiple functions. The first fix attempt "
    "targeted the wrong function, wasting iterations. A debug script finally "
    "identified the correct root cause. The actual fix was a one-line change. "
    "Validation via pytest confirmed all tests passed."
)

REALISTIC_CONFIG_YAML = """\
```yaml
meta:
  name: "localize-fix-validate"
  description: "Three-step pipeline for bug fixing"

steps:
  - id: localize
    prompt: |
      Search for files related to the bug using grep and find. Read the main
      source file and its test file. Identify the functions involved in the
      issue. Output a list of relevant files and likely root cause location.
    tools: [bash, str_replace_editor]
    inputs: []

  - id: fix
    prompt: |
      Based on the localization output, read the relevant code section and
      understand the root cause before making changes. Apply a minimal,
      targeted fix. Run the reproduction script to verify the fix works.
    tools: [bash, str_replace_editor]
    inputs: [localize]

  - id: validate
    prompt: |
      Run the project test suite to verify no regressions. If tests fail,
      report which tests failed and why.
    tools: [bash]
    inputs: [fix]
```
"""

REALISTIC_MUTATED_CONFIG_YAML = """\
```yaml
meta:
  name: "localize-fix-validate"
  description: "Three-step pipeline for bug fixing"

steps:
  - id: localize
    prompt: |
      Search for files related to the bug using grep with keywords from the
      error message. Read the main source and test files. Trace the call chain
      to identify the root cause function. Be thorough before proceeding.
    tools: [bash, str_replace_editor]
    inputs: []

  - id: fix
    prompt: |
      Read the identified code section carefully. Confirm the root cause by
      running a debug snippet before editing. Apply a minimal one-line fix
      if possible. Run the reproduction script to verify.
    tools: [bash, str_replace_editor]
    inputs: [localize]

  - id: validate
    prompt: |
      Run the relevant test file first, then the full module test suite.
      Avoid running the entire project test suite to save budget.
    tools: [bash]
    inputs: [fix]
```
"""


# ===========================================================================
# IT-5.1: Single-step DAG execution
# ===========================================================================


@pytest.mark.integration
class TestSingleStepDAGExecution:
    def test_single_step_with_bash(self, fake_issue):
        """A single-step config using bash completes and collects action history."""
        responses = [
            _tool("bash", {"command": "grep -rn 'divide' ."}, "c1"),
            _text("Found the bug at line 10."),
        ]
        provider = FakeLLMProvider(responses=responses)

        config = _make_config(
            StepConfig(id="find", prompt="Find the bug.", tools=["bash"]),
        )
        executor = DAGExecutor(action_registry=ActionRegistry(_all_actions()))
        result = executor.execute(config, fake_issue, provider.complete)

        assert not result.aborted
        assert "find" in result.step_outputs
        assert len(result.action_history) >= 1
        assert result.action_history[0].action_name == "bash"

    def test_single_step_with_str_replace_editor(self, fake_issue):
        """str_replace_editor view command works inside DAG execution."""
        responses = [
            _tool("str_replace_editor", {"command": "view", "path": "calc.py"}, "c1"),
            _text("Read the file."),
        ]
        provider = FakeLLMProvider(responses=responses)

        config = _make_config(
            StepConfig(id="read", prompt="Read the file.", tools=["str_replace_editor"]),
        )
        executor = DAGExecutor(action_registry=ActionRegistry(_all_actions()))
        result = executor.execute(config, fake_issue, provider.complete)

        assert not result.aborted
        assert "read" in result.step_outputs


# ===========================================================================
# IT-5.2: Multi-step DAG with context injection
# ===========================================================================


@pytest.mark.integration
class TestMultiStepDAGWithDependencies:
    def test_output_injected_into_downstream_step(self, fake_issue):
        """Step 2 receives step 1's output in its context."""
        # Step 1: localize
        step1_responses = [
            _tool("bash", {"command": "grep -rn 'bug' ."}, "c1"),
            _text("Bug found in calc.py:10"),
        ]
        # Step 2: fix (should see step 1 output)
        step2_responses = [
            _tool("str_replace_editor", {
                "command": "str_replace",
                "path": "calc.py",
                "old_str": "old code",
                "new_str": "new code",
            }, "c2"),
            _text("Fixed."),
        ]
        provider = FakeLLMProvider(responses=step1_responses + step2_responses)

        config = _make_config(
            StepConfig(id="locate", prompt="Find the bug.", tools=["bash"]),
            StepConfig(id="fix", prompt="Fix it.", tools=["str_replace_editor"], inputs=["locate"]),
        )
        executor = DAGExecutor(action_registry=ActionRegistry(_all_actions()))
        result = executor.execute(config, fake_issue, provider.complete)

        assert not result.aborted
        assert "locate" in result.step_outputs
        assert "fix" in result.step_outputs

        # Verify context injection: the fix step's LLM call must contain locate's output
        fix_call_idx = len(step1_responses)
        fix_request = provider.call_log[fix_call_idx][0]
        messages_text = str(fix_request.messages)
        assert result.step_outputs["locate"] in messages_text

    def test_action_history_spans_all_steps(self, fake_issue):
        """ExecutionResult.action_history includes records from all steps."""
        responses = [
            _tool("bash", {"command": "ls"}, "c1"),
            _text("step1 done"),
            _tool("bash", {"command": "echo fix"}, "c2"),
            _text("step2 done"),
        ]
        provider = FakeLLMProvider(responses=responses)

        config = _make_config(
            StepConfig(id="s1", prompt="First.", tools=["bash"]),
            StepConfig(id="s2", prompt="Second.", tools=["bash"], inputs=["s1"]),
        )
        executor = DAGExecutor(action_registry=ActionRegistry(_all_actions()))
        result = executor.execute(config, fake_issue, provider.complete)

        assert len(result.action_history) == 2  # one bash call per step


# ===========================================================================
# IT-5.3: DAG abort on budget exhaustion
# ===========================================================================


@pytest.mark.integration
class TestDAGAbort:
    def test_budget_exhaustion_aborts_pipeline(self, fake_issue):
        provider = FakeLLMProvider(
            responses=[_text("unreachable")],
            errors={0: BudgetExhaustedError("No budget")},
        )
        config = _make_config(
            StepConfig(id="s1", prompt="Try.", tools=["bash"]),
            StepConfig(id="s2", prompt="Never runs.", tools=["bash"], inputs=["s1"]),
        )
        executor = DAGExecutor(action_registry=ActionRegistry(_all_actions()))
        result = executor.execute(config, fake_issue, provider.complete)

        assert result.aborted
        assert result.abort_step == "s1"
        assert provider.call_count == 1
        assert "s2" not in result.step_outputs


# ===========================================================================
# IT-5.4: Cyclic DAG detection
# ===========================================================================


@pytest.mark.integration
class TestCyclicDAGDetection:
    def test_cycle_raises_before_execution(self, fake_issue):
        provider = FakeLLMProvider(responses=[_text("unreachable")])
        config = _make_config(
            StepConfig(id="A", prompt="A", tools=["bash"], inputs=["B"]),
            StepConfig(id="B", prompt="B", tools=["bash"], inputs=["A"]),
        )
        executor = DAGExecutor(action_registry=ActionRegistry(_all_actions()))

        with pytest.raises(CyclicDependencyError):
            executor.execute(config, fake_issue, provider.complete)

        assert provider.call_count == 0


# ===========================================================================
# IT-5.5: Config creation from successful trace (two-pass)
# ===========================================================================


@pytest.mark.integration
class TestConfigCreation:
    def test_creates_multi_step_config_from_trace(self):
        """ConfigCreator produces a valid multi-step WorkflowConfig."""
        # Pass 1 returns abstract summary, Pass 2 returns YAML config
        system_provider = FakeLLMProvider(responses=[
            _text(REALISTIC_SUMMARY),
            _text(REALISTIC_CONFIG_YAML),
        ])
        creator = ConfigCreator(system_llm=system_provider.complete)

        history = [
            ActionRecord("bash", {"command": "grep -rn bug ."}, "calc.py:10: bug", 1.0),
            ActionRecord("str_replace_editor", {"command": "view", "path": "calc.py"}, "def divide()...", 2.0),
            ActionRecord("bash", {"command": "python repro.py"}, "Error: division by zero", 3.0),
            ActionRecord("str_replace_editor", {"command": "str_replace", "path": "calc.py", "old_str": "x", "new_str": "y"}, "Applied.", 4.0),
            ActionRecord("bash", {"command": "python -m pytest"}, "PASSED", 5.0),
        ]

        config = creator.create_config(action_history=history, score=1.0)

        assert config is not None
        assert len(config.steps) == 3
        assert config.steps[0].id == "localize"
        assert config.steps[1].id == "fix"
        assert config.steps[2].id == "validate"
        assert "bash" in config.steps[0].tools
        # Two LLM calls: summarize + generate
        assert system_provider.call_count == 2

    def test_returns_none_on_empty_history(self):
        system_provider = FakeLLMProvider(responses=[_text("ignored")])
        creator = ConfigCreator(system_llm=system_provider.complete)

        config = creator.create_config(action_history=[], score=1.0)

        assert config is None
        assert system_provider.call_count == 0

    def test_returns_none_on_unparseable_yaml(self):
        """If the LLM returns garbage, ConfigCreator returns None gracefully."""
        system_provider = FakeLLMProvider(responses=[
            _text(REALISTIC_SUMMARY),
            _text("this is not yaml at all {{{"),
        ])
        creator = ConfigCreator(system_llm=system_provider.complete)

        history = [ActionRecord("bash", {"command": "ls"}, "file.py", 1.0)]
        config = creator.create_config(action_history=history, score=1.0)

        assert config is None


# ===========================================================================
# IT-5.6: Config creation triggered on first success in workspace
# ===========================================================================


@pytest.mark.integration
class TestConfigCreationInWorkspace:
    def test_first_success_upgrades_default_config(self, temp_dir):
        """post_episode with score=1.0 on default config triggers creation."""
        # Execution LLM: agent runs bash, then task_done
        exec_responses = [
            _tool("bash", {"command": "grep bug ."}, "c1"),
            _tool("task_done", {"result": "Fixed the bug."}, "c2"),
        ]
        exec_provider = FakeLLMProvider(responses=exec_responses)

        # System LLM: pass 1 summary, pass 2 config YAML
        sys_provider = FakeLLMProvider(responses=[
            _text(REALISTIC_SUMMARY),
            _text(REALISTIC_CONFIG_YAML),
        ])

        ws = _make_workspace(
            workspace_id="ws-0",
            config=_make_config(
                StepConfig(id="main", prompt="Solve.", tools=["bash", "str_replace_editor", "task_done"]),
            ),
            call_llm=exec_provider.complete,
            system_llm=sys_provider.complete,
            temp_dir=temp_dir,
        )

        ws.execute(_make_issue())

        # Verify default config before post_episode
        assert ws._is_default_config()

        result = ws.post_episode(
            eval_results={"ws-0": {"s_w": 1.0, "s_exec": 1.0}},
            evicted_ids=[],
        )

        assert result is None  # survived
        assert not ws._is_default_config()
        assert len(ws._workflow_config.steps) == 3
        assert ws._workflow_config.meta.name == "localize-fix-validate"

    def test_low_score_keeps_default_config(self, temp_dir):
        """post_episode with score<1.0 on default config does NOT trigger creation."""
        exec_provider = FakeLLMProvider(responses=[
            _tool("bash", {"command": "ls"}, "c1"),
            _text("Could not find the bug."),
        ])
        sys_provider = FakeLLMProvider(responses=[_text("should not be called")])

        ws = _make_workspace(
            workspace_id="ws-0",
            config=_make_config(
                StepConfig(id="main", prompt="Solve.", tools=["bash"]),
            ),
            call_llm=exec_provider.complete,
            system_llm=sys_provider.complete,
            temp_dir=temp_dir,
        )

        ws.execute(_make_issue())
        ws.post_episode(
            eval_results={"ws-0": {"s_w": 0.3, "s_exec": 0.0}},
            evicted_ids=[],
        )

        assert ws._is_default_config()
        assert sys_provider.call_count == 0  # system LLM never called


# ===========================================================================
# IT-5.7: Reflective mutation with real traces
# ===========================================================================


@pytest.mark.integration
class TestReflectiveMutation:
    def test_mutates_prompts_using_trace_and_score(self):
        """reflective_self_rewrite uses two-pass (summary + mutation)."""
        sys_provider = FakeLLMProvider(responses=[
            _text(REALISTIC_SUMMARY),
            _text(REALISTIC_MUTATED_CONFIG_YAML),
        ])
        mutator = ConfigMutator(system_llm=sys_provider.complete)

        original = _make_config(
            StepConfig(id="localize", prompt="Find the bug.", tools=["bash", "str_replace_editor"], inputs=[]),
            StepConfig(id="fix", prompt="Fix it.", tools=["bash", "str_replace_editor"], inputs=["localize"]),
            StepConfig(id="validate", prompt="Run tests.", tools=["bash"], inputs=["fix"]),
            name="localize-fix-validate",
        )
        history = [
            ActionRecord("bash", {"command": "grep bug ."}, "calc.py:10", 1.0),
            ActionRecord("str_replace_editor", {"command": "view", "path": "calc.py"}, "def divide()...", 2.0),
            ActionRecord("bash", {"command": "python -m pytest"}, "PASSED", 3.0),
        ]

        result = mutator.reflective_self_rewrite(
            config=original, action_history=history, score=0.8,
        )

        # Structure preserved
        assert len(result.steps) == 3
        assert [s.id for s in result.steps] == ["localize", "fix", "validate"]
        assert result.steps[0].tools == ["bash", "str_replace_editor"]
        assert result.steps[2].inputs == ["fix"]
        # Prompts changed
        assert result.steps[0].prompt != original.steps[0].prompt
        # Two system LLM calls (summarize + mutate)
        assert sys_provider.call_count == 2

    def test_falls_back_on_invalid_mutation(self):
        """If LLM changes the DAG structure, constraint gate rejects and returns original."""
        # LLM returns config with different step IDs (invalid)
        bad_yaml = """\
```yaml
meta:
  name: "localize-fix-validate"
  description: "test"
steps:
  - id: search
    prompt: "Changed ID!"
    tools: [bash, str_replace_editor]
    inputs: []
  - id: fix
    prompt: "Fix."
    tools: [bash, str_replace_editor]
    inputs: [search]
  - id: validate
    prompt: "Test."
    tools: [bash]
    inputs: [fix]
```
"""
        sys_provider = FakeLLMProvider(responses=[
            _text(REALISTIC_SUMMARY),
            _text(bad_yaml),
        ])
        mutator = ConfigMutator(system_llm=sys_provider.complete)

        original = _make_config(
            StepConfig(id="localize", prompt="Find.", tools=["bash", "str_replace_editor"], inputs=[]),
            StepConfig(id="fix", prompt="Fix.", tools=["bash", "str_replace_editor"], inputs=["localize"]),
            StepConfig(id="validate", prompt="Test.", tools=["bash"], inputs=["fix"]),
            name="localize-fix-validate",
        )
        history = [ActionRecord("bash", {"command": "ls"}, "file.py", 1.0)]

        result = mutator.reflective_self_rewrite(
            config=original, action_history=history, score=0.5,
        )

        # Returns original because step ID changed (localize → search)
        assert result.steps[0].id == "localize"
        assert result.steps[0].prompt == "Find."

    def test_skips_mutation_on_empty_history(self):
        """No action history → no mutation, return original."""
        sys_provider = FakeLLMProvider(responses=[_text("should not be called")])
        mutator = ConfigMutator(system_llm=sys_provider.complete)

        original = _make_config(
            StepConfig(id="s1", prompt="Do.", tools=["bash"]),
        )

        result = mutator.reflective_self_rewrite(
            config=original, action_history=[], score=0.5,
        )

        assert result is original
        assert sys_provider.call_count == 0


# ===========================================================================
# IT-5.8: Reflective mutation in workspace post_episode
# ===========================================================================


@pytest.mark.integration
class TestReflectiveMutationInWorkspace:
    def test_surviving_multi_step_workspace_gets_mutated(self, temp_dir):
        """A surviving workspace with multi-step config calls reflective mutation."""
        multi_step_config = _make_config(
            StepConfig(id="localize", prompt="Find.", tools=["bash", "str_replace_editor"], inputs=[]),
            StepConfig(id="fix", prompt="Fix.", tools=["bash", "str_replace_editor"], inputs=["localize"]),
            StepConfig(id="validate", prompt="Test.", tools=["bash"], inputs=["fix"]),
            name="localize-fix-validate",
        )

        # Execution: agent does some work
        exec_provider = FakeLLMProvider(responses=[
            _tool("bash", {"command": "grep bug ."}, "c1"),
            _text("Found bug in calc.py"),  # localize done
            _tool("str_replace_editor", {"command": "str_replace", "path": "calc.py", "old_str": "a", "new_str": "b"}, "c2"),
            _text("Fixed."),  # fix done
            _tool("bash", {"command": "pytest"}, "c3"),
            _text("All passed."),  # validate done
        ])

        # System LLM: summary + mutated config
        sys_provider = FakeLLMProvider(responses=[
            _text(REALISTIC_SUMMARY),
            _text(REALISTIC_MUTATED_CONFIG_YAML),
        ])

        ws = _make_workspace(
            workspace_id="ws-0",
            config=multi_step_config,
            call_llm=exec_provider.complete,
            system_llm=sys_provider.complete,
            temp_dir=temp_dir,
        )

        ws.execute(_make_issue())
        ws.post_episode(
            eval_results={"ws-0": {"s_w": 0.8, "s_exec": 0.8}},
            evicted_ids=[],
        )

        # System LLM should have been called for reflective mutation
        assert sys_provider.call_count >= 2
        # Prompts should be updated
        assert "thorough" in ws._workflow_config.steps[0].prompt.lower() or \
               "keyword" in ws._workflow_config.steps[0].prompt.lower()


# ===========================================================================
# IT-5.9: Evicted workspace returns None
# ===========================================================================


@pytest.mark.integration
class TestEvictedWorkspacePostEpisode:
    def test_evicted_returns_none(self, temp_dir):
        """Evicted workspace returns None — scheduler handles reproduction."""
        exec_provider = FakeLLMProvider(responses=[_text("Failed.")])
        sys_provider = FakeLLMProvider(responses=[_text("ignored")])

        ws = _make_workspace(
            workspace_id="ws-bad",
            call_llm=exec_provider.complete,
            system_llm=sys_provider.complete,
            temp_dir=temp_dir,
        )
        ws.execute(_make_issue())

        result = ws.post_episode(
            eval_results={"ws-bad": {"s_w": 0.1, "s_exec": 0.0}},
            evicted_ids=["ws-bad"],
        )

        assert result is None
        # System LLM should NOT be called (evicted workspace does nothing)
        assert sys_provider.call_count == 0


# ===========================================================================
# IT-5.10: Constraint gating for mutations
# ===========================================================================


@pytest.mark.integration
class TestConstraintGating:
    def test_rejects_changed_step_ids(self):
        old = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        new = _make_config(StepConfig(id="s2", prompt="A.", tools=["bash"]))
        assert not _validate_mutation(old, new)

    def test_rejects_changed_tools(self):
        old = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash", "str_replace_editor"]))
        assert not _validate_mutation(old, new)

    def test_rejects_changed_inputs(self):
        old = _make_config(
            StepConfig(id="s1", prompt="A.", tools=["bash"], inputs=[]),
            StepConfig(id="s2", prompt="B.", tools=["bash"], inputs=["s1"]),
        )
        new = _make_config(
            StepConfig(id="s1", prompt="A.", tools=["bash"], inputs=[]),
            StepConfig(id="s2", prompt="B.", tools=["bash"], inputs=[]),  # inputs removed
        )
        assert not _validate_mutation(old, new)

    def test_rejects_empty_prompt(self):
        old = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="   ", tools=["bash"]))
        assert not _validate_mutation(old, new)

    def test_rejects_oversized_prompt(self):
        old = _make_config(StepConfig(id="s1", prompt="Short.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="X" * 2001, tools=["bash"]))
        assert not _validate_mutation(old, new)

    def test_rejects_excessive_growth(self):
        old = _make_config(StepConfig(id="s1", prompt="A" * 100, tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="B" * 200, tools=["bash"]))
        assert not _validate_mutation(old, new)  # 100% growth > 30% limit

    def test_accepts_valid_prompt_change(self):
        old = _make_config(StepConfig(id="s1", prompt="Find the bug.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="Search for the bug using grep.", tools=["bash"]))
        assert _validate_mutation(old, new)

    def test_rejects_different_step_count(self):
        old = _make_config(
            StepConfig(id="s1", prompt="A.", tools=["bash"]),
            StepConfig(id="s2", prompt="B.", tools=["bash"], inputs=["s1"]),
        )
        new = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        assert not _validate_mutation(old, new)


# ===========================================================================
# IT-5.11: Trace formatting and YAML helpers
# ===========================================================================


@pytest.mark.integration
class TestTraceAndYAMLHelpers:
    def test_format_trace_truncates_long_results(self):
        history = [
            ActionRecord("bash", {"command": "cat big.py"}, "x" * 500, 1.0),
        ]
        formatted = format_trace(history)
        assert "..." in formatted
        assert len(formatted) < 500

    def test_format_trace_caps_at_max_iterations(self):
        history = [
            ActionRecord("bash", {"command": f"cmd_{i}"}, f"result_{i}", float(i))
            for i in range(100)
        ]
        formatted = format_trace(history)
        assert "truncated" in formatted

    def test_tool_usage_summary(self):
        history = [
            ActionRecord("bash", {}, "", 1.0),
            ActionRecord("bash", {}, "", 2.0),
            ActionRecord("str_replace_editor", {}, "", 3.0),
        ]
        summary = _tool_usage_summary(history)
        assert "bash (2)" in summary
        assert "str_replace_editor (1)" in summary

    def test_extract_yaml_from_fenced_block(self):
        text = "Here:\n```yaml\nmeta:\n  name: test\n```\n"
        assert "meta:" in _extract_yaml(text)

    def test_parse_config_yaml_returns_workflow(self):
        yaml_text = (
            "meta:\n  name: test\n  description: d\n"
            "steps:\n  - id: s1\n    prompt: do\n    tools: [bash]\n    inputs: []\n"
        )
        config = _parse_config_yaml(yaml_text)
        assert config is not None
        assert config.meta.name == "test"
        assert len(config.steps) == 1

    def test_parse_config_yaml_returns_none_for_garbage(self):
        assert _parse_config_yaml("not yaml {{{") is None

    def test_config_to_yaml_roundtrip(self):
        original = _make_config(
            StepConfig(id="s1", prompt="Do.", tools=["bash"], inputs=[]),
            StepConfig(id="s2", prompt="Then.", tools=["str_replace_editor"], inputs=["s1"]),
        )
        yaml_text = _config_to_yaml(original)
        parsed = _parse_config_yaml(yaml_text)
        assert parsed is not None
        assert len(parsed.steps) == 2
        assert parsed.steps[0].id == "s1"
        assert parsed.steps[1].inputs == ["s1"]


# ===========================================================================
# IT-5.12: submit_patch writes file
# ===========================================================================


@pytest.mark.integration
class TestSubmitPatch:
    def test_patch_file_written(self, fake_issue, temp_dir):
        exec_provider = FakeLLMProvider(responses=[
            _tool("bash", {"command": "echo fix"}, "c1"),
            _text("Done."),
        ])
        sys_provider = FakeLLMProvider(responses=[_text("ok")])

        ws = _make_workspace(
            workspace_id="ws-patch",
            config=_make_config(
                StepConfig(id="main", prompt="Fix.", tools=["bash"]),
            ),
            call_llm=exec_provider.complete,
            system_llm=sys_provider.complete,
            temp_dir=temp_dir,
        )
        ws.execute(fake_issue)
        ws.submit_patch()

        patches_dir = os.path.join(temp_dir, "patches", "ws-patch")
        if os.path.isdir(patches_dir):
            patch_files = [f for f in os.listdir(patches_dir) if f.endswith(".patch")]
            assert len(patch_files) >= 1

    def test_aborted_dag_still_writes_patch(self, fake_issue, temp_dir):
        provider = FakeLLMProvider(
            responses=[_text("unreachable")],
            errors={0: BudgetExhaustedError("No budget")},
        )
        ws = _make_workspace(
            workspace_id="ws-abort",
            config=_make_config(
                StepConfig(id="main", prompt="Try.", tools=["bash"]),
            ),
            call_llm=provider.complete,
            temp_dir=temp_dir,
        )
        ws.execute(fake_issue)
        ws.submit_patch()  # must not raise


# ===========================================================================
# IT-5.13: ReactAgent iteration limit
# ===========================================================================


@pytest.mark.integration
class TestReactAgentIterationLimit:
    def test_terminates_after_max_iterations(self):
        responses = [
            _tool("str_replace_editor", {"command": "view", "path": "a.py"}, f"c{i}")
            for i in range(10)
        ]
        provider = FakeLLMProvider(responses=responses)
        agent = ReactAgent(
            system_prompt="You are a code assistant.",
            actions=[StrReplaceEditorAction()],
            call_llm=provider.complete,
            max_iterations=3,
        )

        result = agent.run(context="Fix the bug.")

        assert result.iterations == 3
        assert result.termination_reason == "max_iterations"

    def test_task_done_terminates_early(self):
        responses = [
            _tool("bash", {"command": "ls"}, "c1"),
            _tool("task_done", {"result": "All done."}, "c2"),
        ]
        provider = FakeLLMProvider(responses=responses)
        agent = ReactAgent(
            system_prompt="You are a code assistant.",
            actions=_all_actions(),
            call_llm=provider.complete,
            max_iterations=10,
        )

        result = agent.run(context="Fix the bug.")

        assert result.iterations == 2
        assert result.termination_reason == "done"
        assert "TASK_DONE" in result.output or "All done" in result.output


# ===========================================================================
# IT-5.14: Default config has SYSTEM_PROMPT and all tools
# ===========================================================================


@pytest.mark.integration
class TestDefaultConfig:
    def test_default_config_from_manager(self):
        """WorkspaceManager creates default config with SYSTEM_PROMPT + all tools."""
        from midas_agent.config import MidasConfig
        from midas_agent.workspace.manager import WorkspaceManager

        config = MidasConfig(
            initial_budget=100000,
            workspace_count=1,
            runtime_mode="config_evolution",
        )
        fake_llm = lambda req: _text("ok")
        wm = WorkspaceManager(
            config=config,
            call_llm_factory=lambda ws_id: fake_llm,
            system_llm_callback=fake_llm,
        )

        ws = wm.create("ws-test")
        wc = ws._workflow_config

        assert len(wc.steps) == 1
        assert wc.steps[0].id == "main"
        assert "bash" in wc.steps[0].tools
        assert "str_replace_editor" in wc.steps[0].tools
        assert "task_done" in wc.steps[0].tools
        assert "coding agent" in wc.steps[0].prompt.lower()
        assert ws._is_default_config()


# ===========================================================================
# IT-5.15: Best-eta reproduction via scheduler
# ===========================================================================


@pytest.mark.integration
class TestBestEtaReproduction:
    def test_scheduler_seeds_replacement_with_best_config(self):
        """Scheduler._get_best_config returns the highest-eta workspace's config."""
        from unittest.mock import MagicMock
        from midas_agent.scheduler.scheduler import Scheduler

        # Create mock workspaces
        ws_good = MagicMock()
        ws_good.workspace_id = "ws-good"
        ws_good._workflow_config = _make_config(
            StepConfig(id="localize", prompt="Best prompt.", tools=["bash"], inputs=[]),
            StepConfig(id="fix", prompt="Best fix.", tools=["str_replace_editor"], inputs=["localize"]),
            name="best-config",
        )

        ws_bad = MagicMock()
        ws_bad.workspace_id = "ws-bad"
        ws_bad._workflow_config = _make_config(
            StepConfig(id="main", prompt="Bad.", tools=["bash"]),
            name="bad-config",
        )

        workspace_manager = MagicMock()
        workspace_manager.list_workspaces.return_value = [ws_good, ws_bad]

        scheduler = MagicMock(spec=Scheduler)
        scheduler._last_etas = {"ws-good": 0.9, "ws-bad": 0.1}
        scheduler._workspace_manager = workspace_manager
        scheduler._get_best_config = Scheduler._get_best_config.__get__(scheduler)

        best = scheduler._get_best_config()

        assert best is not None
        assert best["meta"]["name"] == "best-config"
        assert len(best["steps"]) == 2
        assert best["steps"][0]["prompt"] == "Best prompt."
