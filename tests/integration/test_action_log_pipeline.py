"""Integration tests for action log pipeline: workspace and TUI produce JSONL."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.action import Action
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.types import Issue
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.skill import SkillReviewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBashAction(Action):
    """Bash action stub for integration tests."""

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Run bash"

    @property
    def parameters(self) -> dict:
        return {"command": {"type": "string", "required": True}}

    def execute(self, **kwargs) -> str:
        return "fake output"


def _usage(n: int = 10) -> TokenUsage:
    return TokenUsage(input_tokens=n, output_tokens=n)


def _make_llm_response(content="ok", tool_calls=None):
    return LLMResponse(content=content, tool_calls=tool_calls, usage=_usage())


def _task_done_response(result="done"):
    return _make_llm_response(
        content=None,
        tool_calls=[ToolCall(id="tc-done", name="task_done", arguments={"result": result})],
    )


def _bash_then_done_responses():
    """LLM responses: one bash call, then task_done."""
    return [
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})],
            usage=_usage(),
        ),
        # PlanExecuteAgent planning phase response (content only, no tool calls)
        _make_llm_response(content="Plan: run bash then finish"),
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c2", name="bash", arguments={"command": "echo world"})],
            usage=_usage(),
        ),
        _task_done_response("all done"),
    ]


FAKE_ISSUE = Issue(
    issue_id="issue-test-log",
    repo="tests/fixtures/sample_repo",
    description="Test issue for action log.",
    fail_to_pass=["tests/test_x.py::test_a"],
    pass_to_pass=[],
)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTrainingEpisodeActionLog:
    """Workspace passes action_log through to the agent."""

    def test_training_episode_produces_action_log(self, tmp_path):
        """After ws.execute(issue), the action_log file contains valid JSONL entries."""
        from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
        from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace

        log_path = tmp_path / "action_log.jsonl"

        # Scripted LLM that calls bash then task_done
        call_count = 0
        responses = _bash_then_done_responses()

        def scripted_llm(req: LLMRequest) -> LLMResponse:
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx < len(responses):
                return responses[idx]
            return responses[-1]

        # Build minimal workspace
        agent = Agent(
            agent_id="resp-1",
            soul=Soul(system_prompt="You are a test agent."),
            agent_type="workspace_bound",
        )

        pricing = MagicMock()
        pricing.calculate_price = MagicMock(return_value=100)
        fam = FreeAgentManager(pricing_engine=pricing)
        skill_reviewer = MagicMock(spec=SkillReviewer)
        skill_reviewer.review = MagicMock()

        with open(log_path, "w") as action_log_file:
            ws = GraphEmergenceWorkspace(
                workspace_id="ws-test-log",
                responsible_agent=agent,
                call_llm=scripted_llm,
                system_llm=scripted_llm,
                free_agent_manager=fam,
                skill_reviewer=skill_reviewer,
                action_overrides={"bash": FakeBashAction()},
                action_log=action_log_file,
            )
            ws.receive_budget(100000)
            ws.execute(FAKE_ISSUE)

        # Verify JSONL file has content
        assert log_path.exists()
        text = log_path.read_text()
        lines = [line for line in text.strip().splitlines() if line.strip()]
        assert len(lines) >= 1, f"Expected at least 1 JSONL line, got {len(lines)}"

        # Each line is valid JSON with expected fields
        for line in lines:
            entry = json.loads(line)
            assert "iter" in entry
            assert "action" in entry
            assert "result" in entry
            assert "timestamp" in entry


@pytest.mark.integration
class TestTUISessionActionLog:
    """TUI passes action_log through to the ReactAgent it creates."""

    def test_tui_session_produces_action_log(self, tmp_path):
        """TUI with action_log file produces JSONL entries for executed actions."""
        from midas_agent.tui import TUI

        log_path = tmp_path / "tui_action_log.jsonl"

        call_llm = MagicMock(return_value=_task_done_response("fixed"))

        with open(log_path, "w") as action_log_file:
            tui = TUI(
                call_llm=call_llm,
                actions=[FakeBashAction(), TaskDoneAction()],
                system_prompt="test agent",
                action_log=action_log_file,
            )

            with patch("builtins.input", side_effect=["Fix the bug", "/quit"]):
                tui.run()

        # Verify JSONL file has content
        assert log_path.exists()
        text = log_path.read_text()
        lines = [line for line in text.strip().splitlines() if line.strip()]
        assert len(lines) >= 1, f"Expected at least 1 JSONL line, got {len(lines)}"

        # Each line is valid JSON
        for line in lines:
            entry = json.loads(line)
            assert "action" in entry
            assert "timestamp" in entry
            assert isinstance(entry["timestamp"], float)


# ---------------------------------------------------------------------------
# Real training pipeline tests — expected to FAIL because run_training()
# and _cmd_train() do not yet wire action_log through to workspaces.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestActionLogInRealPipeline:
    """Action log must be produced by the real training pipeline, not just by manual wiring."""

    def test_run_training_produces_action_log(self, tmp_path):
        """The real training pipeline (Scheduler + WorkspaceManager) must
        create JSONL action log files for each workspace episode.

        This exercises the same flow as run_training() but without Docker /
        SWE-bench, using the same technique as test_full_episode.py: build
        a Scheduler with fakes and run the episode steps manually.

        EXPECTED TO FAIL: WorkspaceManager._create_graph_emergence_workspace()
        does not pass action_log to GraphEmergenceWorkspace, so no JSONL is
        produced.
        """
        from midas_agent.config import MidasConfig
        from midas_agent.evaluation.criteria_cache import CriteriaCache
        from midas_agent.evaluation.llm_judge import LLMJudge
        from midas_agent.evaluation.module import EvaluationModule
        from midas_agent.scheduler.budget_allocator import (
            AdaptiveMultiplier,
            BudgetAllocator,
        )
        from midas_agent.scheduler.resource_meter import ResourceMeter
        from midas_agent.scheduler.scheduler import Scheduler
        from midas_agent.scheduler.selection import SelectionEngine
        from midas_agent.scheduler.serial_queue import SerialQueue
        from midas_agent.scheduler.system_llm import SystemLLM
        from midas_agent.scheduler.training_log import HookSet, TrainingLog
        from midas_agent.workspace.manager import WorkspaceManager

        from tests.integration.conftest import (
            FakeExecutionScorer,
            FakeLLMProvider,
            InMemoryStorageBackend,
        )

        action_log_dir = tmp_path / "action_logs"
        action_log_dir.mkdir()

        config = MidasConfig(
            initial_budget=50000,
            workspace_count=2,
            runtime_mode="graph_emergence",
            n_evict=0,
            score_floor=0.01,
            multiplier_mode="static",
            multiplier_init=1.0,
            beta=0.3,
        )

        # Realistic LLM responses with tool calls
        responses = [
            # ws-0: planning phase
            LLMResponse(
                content="Plan: search for the bug, fix it, done.",
                tool_calls=None,
                usage=TokenUsage(50, 20),
            ),
            # ws-0: search
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c1", name="search_code", arguments={"pattern": "def buggy"})
                ],
                usage=TokenUsage(100, 50),
            ),
            # ws-0: edit
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c2", name="str_replace_editor", arguments={"command": "str_replace", "path": "/testbed/foo.py", "old_str": "bug", "new_str": "fix"})
                ],
                usage=TokenUsage(150, 50),
            ),
            # ws-0: done
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c3", name="task_done", arguments={"result": "Fixed the bug"})
                ],
                usage=TokenUsage(200, 50),
            ),
        ] * 10  # Repeat to provide enough for both workspaces + eval

        storage = InMemoryStorageBackend()
        hooks = HookSet()
        queue = SerialQueue()
        training_log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)

        fake_llm = FakeLLMProvider(responses=responses)
        fake_scorer = FakeExecutionScorer(scores={"ws-0": 0.7, "ws-1": 0.5})

        resource_meter = ResourceMeter(training_log=training_log, llm_provider=fake_llm)
        system_llm = SystemLLM(llm_provider=fake_llm)

        adaptive_mult = AdaptiveMultiplier(
            mode=config.multiplier_mode,
            init_value=config.multiplier_init,
            er_target=config.er_target,
            cool_down=config.cool_down,
            mult_min=config.mult_min,
            mult_max=config.mult_max,
        )
        budget_allocator = BudgetAllocator(
            score_floor=config.score_floor,
            multiplier_init=config.multiplier_init,
            adaptive_multiplier=adaptive_mult,
        )
        selection_engine = SelectionEngine(
            runtime_mode=config.runtime_mode,
            n_evict=config.n_evict,
        )

        # Use the REAL WorkspaceManager — this is the component under test.
        # The real manager does NOT pass action_log, which is the bug.
        workspace_manager = WorkspaceManager(
            config=config,
            call_llm_factory=lambda ws_id: (
                lambda req: resource_meter.process(req, entity_id=ws_id)
            ),
            system_llm_callback=lambda req: system_llm.call(req),
        )

        cache_dir = str(tmp_path / "criteria_cache")
        os.makedirs(cache_dir, exist_ok=True)
        criteria_cache = CriteriaCache(cache_dir=cache_dir)
        llm_judge = LLMJudge(llm_provider=fake_llm, criteria_cache=criteria_cache)
        evaluation_module = EvaluationModule(
            execution_scorer=fake_scorer,
            llm_judge=llm_judge,
            beta=config.beta,
        )

        scheduler = Scheduler(
            config=config,
            training_log=training_log,
            resource_meter=resource_meter,
            system_llm=system_llm,
            budget_allocator=budget_allocator,
            selection_engine=selection_engine,
            workspace_manager=workspace_manager,
            evaluation_module=evaluation_module,
        )

        # Run one episode (mirrors run_training episode loop)
        issue = Issue(
            issue_id="issue-action-log",
            repo="test/repo",
            description="Fix the divide-by-zero bug in calculator.py",
            fail_to_pass=["tests/test_calc.py::test_div_zero"],
            pass_to_pass=[],
        )

        scheduler.create_workspaces()
        scheduler.set_current_issue(issue)
        scheduler.allocate_budgets()

        workspaces = scheduler.get_workspaces()
        for ws in workspaces:
            ws.execute(issue)

        # After execution, check for JSONL action log files.
        # The training pipeline SHOULD have created action_log files
        # for each workspace. We check that the workspace's internal
        # _action_log attribute is not None (meaning the pipeline wired it).
        action_logs_found = []
        for ws in workspaces:
            action_log_attr = getattr(ws, "_action_log", None)
            if action_log_attr is not None:
                action_logs_found.append(ws.workspace_id)

        assert len(action_logs_found) == len(workspaces), (
            f"Expected action_log to be wired for all {len(workspaces)} workspaces, "
            f"but only found it for: {action_logs_found}. "
            f"run_training() / WorkspaceManager must open JSONL files and pass "
            f"action_log to each workspace."
        )

        # Additionally verify that actual JSONL content was written.
        # Even if action_log were wired, it must contain entries.
        for ws in workspaces:
            log_handle = getattr(ws, "_action_log", None)
            assert log_handle is not None, (
                f"Workspace {ws.workspace_id} has no action_log file handle"
            )

            # If the file handle is available, read its content
            log_handle.flush()
            log_path = log_handle.name
            with open(log_path) as f:
                lines = [l for l in f.read().strip().splitlines() if l.strip()]
            assert len(lines) >= 1, (
                f"Workspace {ws.workspace_id}: action log file is empty, "
                f"expected at least 1 JSONL entry after execute()"
            )

            # Validate structure
            for line in lines:
                entry = json.loads(line)
                assert "action" in entry, f"JSONL entry missing 'action': {entry}"
                assert "timestamp" in entry, f"JSONL entry missing 'timestamp': {entry}"

    def test_cli_cmd_train_produces_action_log(self, tmp_path):
        """_cmd_train() from cli.py must result in action log JSONL files.

        EXPECTED TO FAIL: cli.py -> run_training() does not wire action_log.
        We mock external dependencies (LLM, SWE-bench) but exercise the
        real CLI -> training -> workspace chain.
        """
        from unittest.mock import MagicMock, patch as mock_patch

        from midas_agent.cli import _cmd_train

        action_log_dir = tmp_path / "action_logs"
        action_log_dir.mkdir()

        # Create a minimal training config YAML
        config_yaml = tmp_path / "train_config.yaml"
        config_yaml.write_text(
            "initial_budget: 30000\n"
            "workspace_count: 1\n"
            "runtime_mode: graph_emergence\n"
            "n_evict: 0\n"
            "score_floor: 0.01\n"
            "multiplier_mode: static\n"
            "multiplier_init: 1.0\n"
            "beta: 0.3\n"
        )

        # Realistic LLM responses with tool calls
        scripted_responses = [
            # Planning
            LLMResponse(
                content="Plan: find the bug and fix it.",
                tool_calls=None,
                usage=TokenUsage(50, 20),
            ),
            # Search
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c1", name="search_code", arguments={"pattern": "def buggy"})
                ],
                usage=TokenUsage(100, 50),
            ),
            # Edit
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c2", name="str_replace_editor", arguments={"command": "str_replace", "path": "/testbed/foo.py", "old_str": "bug", "new_str": "fix"})
                ],
                usage=TokenUsage(150, 50),
            ),
            # Done
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c3", name="task_done", arguments={"result": "Fixed"})
                ],
                usage=TokenUsage(200, 50),
            ),
        ] * 20  # Enough for multiple phases

        call_idx = 0

        def fake_complete(request):
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            if idx < len(scripted_responses):
                return scripted_responses[idx]
            return scripted_responses[-1]

        fake_issue = Issue(
            issue_id="issue-cli-log",
            repo="test/repo",
            description="Fix a bug",
            fail_to_pass=["tests/test.py::test_a"],
            pass_to_pass=[],
        )

        # Build a mock args namespace
        args = MagicMock()
        args.config = str(config_yaml)
        args.output = str(tmp_path / "output")
        args.issues = 1

        # Mock resolve_llm_config to avoid needing env vars
        mock_llm_config = MagicMock()
        mock_llm_config.model = ""  # Empty model triggers _StubLLMProvider
        mock_llm_config.api_key = "fake-key"
        mock_llm_config.api_base = ""

        # We need to intercept run_training to inspect the workspaces
        captured_workspaces = []

        original_run_training = None

        def intercepting_run_training(config, issues=None):
            """Wrap run_training to capture workspaces for inspection."""
            from midas_agent.training import run_training as real_run_training
            # We call the real run_training but with our fake issues
            real_run_training(config, issues=issues)

        with mock_patch("midas_agent.resolver.resolve_llm_config", return_value=mock_llm_config):
            with mock_patch("midas_agent.training.load_swe_bench", return_value=[fake_issue]):
                with mock_patch("midas_agent.evaluation.swebench_scorer.SWEBenchScorer") as mock_scorer_cls:
                    mock_scorer = MagicMock()
                    mock_scorer.score.return_value = 0.5
                    mock_scorer_cls.return_value = mock_scorer

                    # Run the actual _cmd_train
                    _cmd_train(args)

        # After training completes, check if action log files were created.
        # The training pipeline should have created JSONL files somewhere
        # under a predictable location (e.g., alongside patches or in a
        # dedicated action_logs directory).
        #
        # Since we cannot inspect workspaces after run_training returns
        # (they are local to that function), we check for the existence
        # of action log files on disk. The pipeline SHOULD create them at
        # a known path (e.g., /tmp/midas_action_logs/ or alongside patches).
        import glob

        # Check various possible locations for action log files
        possible_log_locations = [
            str(tmp_path / "action_logs" / "*.jsonl"),
            "/tmp/midas_action_logs/*.jsonl",
            "/tmp/midas_action_logs/**/*.jsonl",
            "/tmp/patches/**/action_log.jsonl",
        ]

        jsonl_files = []
        for pattern in possible_log_locations:
            jsonl_files.extend(glob.glob(pattern, recursive=True))

        # The real assertion: action log files must exist
        assert len(jsonl_files) >= 1, (
            "Expected at least 1 action log JSONL file to be produced by "
            "_cmd_train() -> run_training(), but none were found. "
            "The training pipeline must open JSONL action log files and "
            "pass them through WorkspaceManager to each workspace. "
            f"Searched: {possible_log_locations}"
        )

        # Validate content of found JSONL files
        for jsonl_path in jsonl_files:
            with open(jsonl_path) as f:
                lines = [l for l in f.read().strip().splitlines() if l.strip()]
            assert len(lines) >= 1, (
                f"Action log {jsonl_path} is empty"
            )
            for line in lines:
                entry = json.loads(line)
                assert "action" in entry
                assert "timestamp" in entry
