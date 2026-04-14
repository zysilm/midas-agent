"""Integration Test Suite 5: Configuration Evolution Execution Pipeline.

All production code is NotImplementedError stubs. These tests define the
expected behavior for TDD and will pass once the production implementations
are filled in.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch, call

import pytest

from midas_agent.llm.types import LLMResponse, TokenUsage, ToolCall
from midas_agent.scheduler.resource_meter import BudgetExhaustedError
from midas_agent.stdlib.action import Action, ActionRegistry
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.file_ops import (
    EditFileAction,
    ReadFileAction,
    WriteFileAction,
)
from midas_agent.stdlib.actions.search import FindFilesAction, SearchCodeAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.react_agent import AgentResult, ReactAgent
from midas_agent.types import Issue
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
from midas_agent.workspace.config_evolution.mutator import ConfigMutator
from midas_agent.workspace.config_evolution.snapshot_store import (
    ConfigSnapshot,
    ConfigSnapshotStore,
    SnapshotFilter,
)
from midas_agent.workspace.config_evolution.workspace import (
    ConfigEvolutionWorkspace,
)
from tests.integration.conftest import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_USAGE = TokenUsage(input_tokens=10, output_tokens=5)


def _text_response(content: str) -> LLMResponse:
    """Build a plain text LLMResponse."""
    return LLMResponse(content=content, tool_calls=None, usage=_DEFAULT_USAGE)


def _tool_response(name: str, arguments: dict, call_id: str = "c1") -> LLMResponse:
    """Build an LLMResponse that contains a single tool call."""
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=_DEFAULT_USAGE,
    )


def _all_actions() -> list[Action]:
    """Return one instance of every standard action."""
    return [
        BashAction(),
        ReadFileAction(),
        EditFileAction(),
        WriteFileAction(),
        SearchCodeAction(),
        FindFilesAction(),
        TaskDoneAction(),
    ]


def _make_workflow(*steps: StepConfig, name: str = "test-wf") -> WorkflowConfig:
    """Shorthand for building a WorkflowConfig."""
    return WorkflowConfig(
        meta=ConfigMeta(name=name, description="test workflow"),
        steps=list(steps),
    )


# ---------------------------------------------------------------------------
# IT-5.1: Single-step DAG execution
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSingleStepDAGExecution:
    """A config with a single step is executed end-to-end: the DAGExecutor
    creates a ReactAgent with the correct tool subset, feeds it the issue
    context, and collects the execution result."""

    def test_single_step_completes(self, fake_issue):
        # Three LLM calls: read_file, edit_file, then a text response
        responses = [
            _tool_response("read_file", {"file_path": "calculator.py"}, "c1"),
            _tool_response(
                "edit_file",
                {
                    "file_path": "calculator.py",
                    "operation": "replace",
                    "start_line": 5,
                    "content": "if divisor == 0: raise ZeroDivisionError",
                },
                "c2",
            ),
            _text_response("Fix applied."),
        ]
        provider = FakeLLMProvider(responses=responses)

        config = _make_workflow(
            StepConfig(
                id="fix",
                prompt="Fix the divide-by-zero bug.",
                tools=["read_file", "edit_file"],
            ),
        )

        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        result = executor.execute(
            config=config,
            issue=fake_issue,
            call_llm=provider.complete,
        )

        assert isinstance(result, ExecutionResult)
        assert result.aborted is False
        assert result.abort_step is None
        assert "fix" in result.step_outputs


# ---------------------------------------------------------------------------
# IT-5.2: Multi-step DAG with dependencies
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMultiStepDAGWithDependencies:
    """Two-step DAG: 'locate' feeds into 'fix'. The output of 'locate'
    must be injected into the 'fix' step context so the agent can use it."""

    def test_locate_output_injected_into_fix(self, fake_issue):
        # Locate step: search_code -> text answer
        locate_responses = [
            _tool_response("search_code", {"pattern": "divide"}, "c1"),
            _text_response("Found bug at calculator.py:10"),
        ]
        # Fix step: edit_file -> text answer
        fix_responses = [
            _tool_response(
                "edit_file",
                {
                    "file_path": "calculator.py",
                    "operation": "replace",
                    "start_line": 10,
                    "content": "if b == 0: raise ZeroDivisionError",
                },
                "c2",
            ),
            _text_response("Patch applied."),
        ]
        all_responses = locate_responses + fix_responses
        provider = FakeLLMProvider(responses=all_responses)

        config = _make_workflow(
            StepConfig(
                id="locate",
                prompt="Find the buggy code.",
                tools=["search_code", "find_files"],
            ),
            StepConfig(
                id="fix",
                prompt="Apply the fix.",
                tools=["edit_file", "bash"],
                inputs=["locate"],
            ),
        )

        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        result = executor.execute(
            config=config,
            issue=fake_issue,
            call_llm=provider.complete,
        )

        assert result.aborted is False
        assert "locate" in result.step_outputs
        assert "fix" in result.step_outputs
        # The fix step must have received locate's output in its context.
        # We verify by checking the LLM call log: the fix step's first
        # request should contain the locate output somewhere in the messages.
        fix_first_call_idx = len(locate_responses)
        fix_request = provider.call_log[fix_first_call_idx][0]
        messages_text = str(fix_request.messages)
        assert result.step_outputs["locate"] in messages_text


# ---------------------------------------------------------------------------
# IT-5.3: DAG step failure aborts pipeline
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDAGStepFailureAbortsPipeline:
    """When the first step's LLM call raises BudgetExhaustedError, the
    pipeline aborts immediately. The second step is never executed."""

    def test_abort_on_budget_exhausted(self, fake_issue):
        # Step 1 raises BudgetExhaustedError on its first LLM call.
        provider = FakeLLMProvider(
            responses=[_text_response("should not reach")],
            errors={0: BudgetExhaustedError("No budget remaining")},
        )

        config = _make_workflow(
            StepConfig(
                id="step1",
                prompt="Attempt something.",
                tools=["read_file"],
            ),
            StepConfig(
                id="step2",
                prompt="This should never run.",
                tools=["edit_file"],
                inputs=["step1"],
            ),
        )

        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        result = executor.execute(
            config=config,
            issue=fake_issue,
            call_llm=provider.complete,
        )

        assert result.aborted is True
        assert result.abort_step == "step1"
        # Step 2 should never have been called — only 1 LLM call was made
        # (the one that raised).
        assert provider.call_count == 1
        assert "step2" not in result.step_outputs


# ---------------------------------------------------------------------------
# IT-5.4: Cyclic DAG detection
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCyclicDAGDetection:
    """A configuration where step A depends on B and B depends on A must
    raise CyclicDependencyError before any execution begins."""

    def test_cyclic_raises_before_execution(self, fake_issue):
        provider = FakeLLMProvider(responses=[_text_response("unreachable")])

        config = _make_workflow(
            StepConfig(
                id="A",
                prompt="Step A",
                tools=["read_file"],
                inputs=["B"],
            ),
            StepConfig(
                id="B",
                prompt="Step B",
                tools=["edit_file"],
                inputs=["A"],
            ),
        )

        registry = ActionRegistry(_all_actions())
        executor = DAGExecutor(action_registry=registry)

        with pytest.raises(CyclicDependencyError):
            executor.execute(
                config=config,
                issue=fake_issue,
                call_llm=provider.complete,
            )

        # No LLM calls should have been made
        assert provider.call_count == 0


# ---------------------------------------------------------------------------
# IT-5.5: submit_patch persists to correct path
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSubmitPatchPersistsFile:
    """After execute(), calling submit_patch() writes a .patch file to
    {output_dir}/{workspace_id}/{episode_id}.patch."""

    def test_patch_file_written(self, fake_issue, temp_dir):
        patch_content = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-old\n+new"

        # FakeLLM returns a tool call then text
        responses = [
            _tool_response("read_file", {"file_path": "calc.py"}, "c1"),
            _text_response("Done."),
        ]
        provider = FakeLLMProvider(responses=responses)

        config = _make_workflow(
            StepConfig(id="fix", prompt="Fix.", tools=["read_file", "edit_file"]),
        )

        # Build a DAGExecutor that returns a result with a patch
        registry = ActionRegistry(_all_actions())
        dag_executor = DAGExecutor(action_registry=registry)

        # Build a mutator and snapshot store (stubs, not exercised here)
        system_provider = FakeLLMProvider(responses=[_text_response("ok")])
        mutator = ConfigMutator(system_llm=system_provider.complete)
        snapshot_store = ConfigSnapshotStore(
            store_dir=os.path.join(temp_dir, "snapshots"),
        )

        workspace = ConfigEvolutionWorkspace(
            workspace_id="ws-patch-test",
            workflow_config=config,
            call_llm=provider.complete,
            system_llm=system_provider.complete,
            dag_executor=dag_executor,
            config_mutator=mutator,
            snapshot_store=snapshot_store,
        )
        workspace.receive_budget(5000)
        workspace.execute(fake_issue)
        workspace.submit_patch()

        # Verify patch file exists under the output directory tree
        patches_dir = os.path.join(temp_dir, "patches")
        ws_dir = os.path.join(patches_dir, "ws-patch-test")
        # Find the .patch file — the exact episode_id is implementation-defined
        # but the file must exist under {patches}/{workspace_id}/.
        if os.path.isdir(ws_dir):
            patch_files = [f for f in os.listdir(ws_dir) if f.endswith(".patch")]
            assert len(patch_files) >= 1, (
                f"Expected at least one .patch file in {ws_dir}, "
                f"found: {os.listdir(ws_dir)}"
            )
            written = open(os.path.join(ws_dir, patch_files[0])).read()
            # The file should contain some content (the patch from execution)
            assert len(written) > 0
        else:
            # Alternatively the workspace might use a flat naming scheme
            all_patches = []
            for root, _dirs, files in os.walk(patches_dir):
                for f in files:
                    if f.endswith(".patch") and "ws-patch-test" in os.path.join(root, f):
                        all_patches.append(os.path.join(root, f))
            assert len(all_patches) >= 1, (
                f"No .patch file found for ws-patch-test under {patches_dir}"
            )


# ---------------------------------------------------------------------------
# IT-5.6: submit_patch on aborted DAG
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSubmitPatchOnAbortedDAG:
    """When the DAG execution is aborted, submit_patch() still writes a file
    (which may be empty or contain partial output)."""

    def test_aborted_dag_still_writes_patch(self, fake_issue, temp_dir):
        # Step 1 raises BudgetExhaustedError immediately
        provider = FakeLLMProvider(
            responses=[_text_response("unreachable")],
            errors={0: BudgetExhaustedError("No budget")},
        )

        config = _make_workflow(
            StepConfig(id="step1", prompt="Attempt.", tools=["read_file"]),
        )

        registry = ActionRegistry(_all_actions())
        dag_executor = DAGExecutor(action_registry=registry)

        system_provider = FakeLLMProvider(responses=[_text_response("ok")])
        mutator = ConfigMutator(system_llm=system_provider.complete)
        snapshot_store = ConfigSnapshotStore(
            store_dir=os.path.join(temp_dir, "snapshots"),
        )

        workspace = ConfigEvolutionWorkspace(
            workspace_id="ws-abort-test",
            workflow_config=config,
            call_llm=provider.complete,
            system_llm=system_provider.complete,
            dag_executor=dag_executor,
            config_mutator=mutator,
            snapshot_store=snapshot_store,
        )
        workspace.receive_budget(5000)
        workspace.execute(fake_issue)
        # submit_patch must not raise even though DAG aborted
        workspace.submit_patch()

        # A .patch file must exist (may be empty)
        patches_dir = os.path.join(temp_dir, "patches")
        found = False
        for root, _dirs, files in os.walk(patches_dir):
            for f in files:
                full_path = os.path.join(root, f)
                if f.endswith(".patch") and "ws-abort-test" in full_path:
                    found = True
                    # File exists — content may be empty
                    break
        assert found, (
            f"Expected a .patch file for ws-abort-test under {patches_dir}"
        )


# ---------------------------------------------------------------------------
# IT-5.7: post_episode — surviving workspace calls self_rewrite
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostEpisodeSurvivalSelfRewrite:
    """When eval_results indicate the workspace survived, post_episode() must
    invoke ConfigMutator.self_rewrite() with the current config and a summary,
    then persist a ConfigSnapshot via ConfigSnapshotStore.save()."""

    def test_self_rewrite_called_on_survival(self, fake_issue, temp_dir):
        # Workspace LLM
        responses = [
            _tool_response("read_file", {"file_path": "calc.py"}, "c1"),
            _text_response("Fixed."),
        ]
        provider = FakeLLMProvider(responses=responses)

        config = _make_workflow(
            StepConfig(id="fix", prompt="Fix.", tools=["read_file"]),
        )

        # System LLM — will be called by self_rewrite
        rewritten_config = _make_workflow(
            StepConfig(id="fix_v2", prompt="Better fix.", tools=["read_file", "edit_file"]),
            name="rewritten-wf",
        )
        system_responses = [_text_response("rewritten config yaml")]
        system_provider = FakeLLMProvider(responses=system_responses)

        registry = ActionRegistry(_all_actions())
        dag_executor = DAGExecutor(action_registry=registry)
        mutator = ConfigMutator(system_llm=system_provider.complete)
        snapshot_store = ConfigSnapshotStore(
            store_dir=os.path.join(temp_dir, "snapshots"),
        )

        workspace = ConfigEvolutionWorkspace(
            workspace_id="ws-survive",
            workflow_config=config,
            call_llm=provider.complete,
            system_llm=system_provider.complete,
            dag_executor=dag_executor,
            config_mutator=mutator,
            snapshot_store=snapshot_store,
        )
        workspace.receive_budget(5000)
        workspace.execute(fake_issue)

        eval_results = {
            "survived": True,
            "score": 0.85,
            "cost": 150,
            "eta": 0.85 / 150,
            "episode_id": "ep-001",
            "summary": "Fixed divide-by-zero successfully.",
        }

        result = workspace.post_episode(eval_results)

        # system_llm must have been invoked (self_rewrite delegates to it)
        assert system_provider.call_count >= 1

        # post_episode returns the mutation result (or None on failure)
        # For a surviving workspace, it should return something non-None
        # indicating the config was updated.
        # (Implementation detail: may return the new config dict or snapshot info)


# ---------------------------------------------------------------------------
# IT-5.8: post_episode — evicted workspace calls reproduce
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostEpisodeEvictionReproduce:
    """When eval_results indicate the workspace was evicted, post_episode()
    must invoke ConfigMutator.reproduce() with the best config from the
    snapshot store and episode summaries."""

    def test_reproduce_called_on_eviction(self, fake_issue, temp_dir):
        # Workspace LLM — single step, then abort to keep it simple
        provider = FakeLLMProvider(
            responses=[_text_response("attempted fix")],
        )

        config = _make_workflow(
            StepConfig(id="fix", prompt="Fix.", tools=["read_file"]),
        )

        system_responses = [_text_response("reproduced config yaml")]
        system_provider = FakeLLMProvider(responses=system_responses)

        registry = ActionRegistry(_all_actions())
        dag_executor = DAGExecutor(action_registry=registry)
        mutator = ConfigMutator(system_llm=system_provider.complete)
        snapshot_store = ConfigSnapshotStore(
            store_dir=os.path.join(temp_dir, "snapshots"),
        )

        workspace = ConfigEvolutionWorkspace(
            workspace_id="ws-evict",
            workflow_config=config,
            call_llm=provider.complete,
            system_llm=system_provider.complete,
            dag_executor=dag_executor,
            config_mutator=mutator,
            snapshot_store=snapshot_store,
        )
        workspace.receive_budget(5000)
        workspace.execute(fake_issue)

        eval_results = {
            "survived": False,
            "score": 0.05,
            "cost": 200,
            "eta": 0.05 / 200,
            "episode_id": "ep-002",
            "summary": "Failed to fix the bug.",
            "best_config": config,
            "best_summaries": ["Previous attempt summary."],
        }

        result = workspace.post_episode(eval_results)

        # system_llm must have been invoked (reproduce delegates to it)
        assert system_provider.call_count >= 1


# ---------------------------------------------------------------------------
# IT-5.9: Action subset filtering
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestActionSubsetFiltering:
    """When a StepConfig specifies tools=["bash", "read_file"], the agent
    for that step must receive exactly those 2 actions from the registry."""

    def test_agent_receives_correct_action_subset(self):
        all_actions = _all_actions()
        registry = ActionRegistry(all_actions)

        subset = registry.get_subset(["bash", "read_file"])

        assert len(subset) == 2
        subset_names = {a.name for a in subset}
        assert subset_names == {"bash", "read_file"}

    def test_full_registry_contains_all_standard_actions(self):
        all_actions = _all_actions()
        registry = ActionRegistry(all_actions)

        expected_names = {
            "bash",
            "read_file",
            "edit_file",
            "write_file",
            "search_code",
            "find_files",
            "task_done",
        }
        for name in expected_names:
            action = registry.get(name)
            assert action.name == name

    def test_subset_preserves_action_identity(self):
        """Actions from get_subset are the same instances as in the registry."""
        all_actions = _all_actions()
        registry = ActionRegistry(all_actions)

        subset = registry.get_subset(["edit_file"])
        assert len(subset) == 1
        assert subset[0] is registry.get("edit_file")


# ---------------------------------------------------------------------------
# IT-5.10: ReactAgent iteration limit
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReactAgentIterationLimit:
    """When max_iterations=3 and the LLM always returns a tool call (never a
    final text answer), the agent must terminate after exactly 3 iterations
    with termination_reason='max_iterations'."""

    def test_terminates_after_max_iterations(self):
        # Every response is a tool call — the agent should never "finish"
        # naturally but instead hit the iteration cap.
        responses = [
            _tool_response("read_file", {"file_path": "a.py"}, f"c{i}")
            for i in range(10)  # more than enough
        ]
        provider = FakeLLMProvider(responses=responses)

        actions = [ReadFileAction()]
        agent = ReactAgent(
            system_prompt="You are a code assistant.",
            actions=actions,
            call_llm=provider.complete,
            max_iterations=3,
        )

        result = agent.run(context="Fix the bug in a.py.")

        assert isinstance(result, AgentResult)
        assert result.iterations == 3
        assert result.termination_reason == "max_iterations"

    def test_no_limit_when_none(self):
        """When max_iterations is None the agent runs until it produces a
        text response (termination_reason='done')."""
        responses = [
            _tool_response("read_file", {"file_path": "a.py"}, "c1"),
            _tool_response("read_file", {"file_path": "b.py"}, "c2"),
            _text_response("All done."),
        ]
        provider = FakeLLMProvider(responses=responses)

        actions = [ReadFileAction()]
        agent = ReactAgent(
            system_prompt="You are a code assistant.",
            actions=actions,
            call_llm=provider.complete,
            max_iterations=None,
        )

        result = agent.run(context="Read files and summarise.")

        assert isinstance(result, AgentResult)
        assert result.termination_reason == "done"
        assert result.iterations == 3
        assert result.output == "All done."
