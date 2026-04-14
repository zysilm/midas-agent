"""Unit tests for the Scheduler facade.

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError.
"""
import pytest
from unittest.mock import MagicMock, PropertyMock, patch

from midas_agent.config import MidasConfig
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.budget_allocator import BudgetAllocator
from midas_agent.scheduler.resource_meter import ResourceMeter
from midas_agent.scheduler.scheduler import Scheduler
from midas_agent.scheduler.selection import SelectionEngine
from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.scheduler.training_log import TrainingLog
from midas_agent.evaluation.module import EvaluationModule, EvalResult
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.manager import WorkspaceManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> MidasConfig:
    defaults = dict(
        initial_budget=10000,
        workspace_count=3,
        runtime_mode="config_evolution",
        score_floor=0.01,
        multiplier_mode="static",
        multiplier_init=1.0,
        n_evict=1,
    )
    defaults.update(overrides)
    return MidasConfig(**defaults)


def _make_scheduler(
    config: MidasConfig | None = None,
    training_log: TrainingLog | None = None,
    resource_meter: ResourceMeter | None = None,
    system_llm: SystemLLM | None = None,
    budget_allocator: BudgetAllocator | None = None,
    selection_engine: SelectionEngine | None = None,
    workspace_manager: WorkspaceManager | None = None,
    evaluation_module: EvaluationModule | None = None,
) -> Scheduler:
    """Build a Scheduler with mock dependencies."""
    return Scheduler(
        config=config or _make_config(),
        training_log=training_log or MagicMock(spec=TrainingLog),
        resource_meter=resource_meter or MagicMock(spec=ResourceMeter),
        system_llm=system_llm or MagicMock(spec=SystemLLM),
        budget_allocator=budget_allocator or MagicMock(spec=BudgetAllocator),
        selection_engine=selection_engine or MagicMock(spec=SelectionEngine),
        workspace_manager=workspace_manager or MagicMock(spec=WorkspaceManager),
        evaluation_module=evaluation_module or MagicMock(spec=EvaluationModule),
    )


def _make_request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "test"}], model="m")


def _make_response() -> LLMResponse:
    return LLMResponse(
        content="ok",
        tool_calls=None,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestScheduler:
    """Tests for the Scheduler facade."""

    def test_construction(self):
        """Scheduler can be constructed with all 8 injected dependencies."""
        scheduler = _make_scheduler()
        assert scheduler is not None

    def test_allocate_budgets(self):
        """allocate_budgets() calls allocator and distributes budgets to workspaces."""
        budget_allocator = MagicMock(spec=BudgetAllocator)
        budget_allocator.calculate_allocation.return_value = {
            "ws-1": 5000,
            "ws-2": 5000,
        }

        ws_1 = MagicMock(spec=Workspace)
        ws_1.workspace_id = "ws-1"
        ws_2 = MagicMock(spec=Workspace)
        ws_2.workspace_id = "ws-2"

        workspace_manager = MagicMock(spec=WorkspaceManager)
        workspace_manager.list_workspaces.return_value = [ws_1, ws_2]

        training_log = MagicMock(spec=TrainingLog)

        scheduler = _make_scheduler(
            budget_allocator=budget_allocator,
            workspace_manager=workspace_manager,
            training_log=training_log,
        )

        # Seed etas so the proportional (non-cold-start) path is taken.
        scheduler._last_etas = {"ws-1": 0.5, "ws-2": 0.5}

        scheduler.allocate_budgets()

        budget_allocator.calculate_allocation.assert_called_once()

    def test_evaluate_and_select_returns_tuple(self):
        """evaluate_and_select() returns (evicted_ids, survivor_ids, eval_results)."""
        eval_module = MagicMock(spec=EvaluationModule)
        eval_result = EvalResult(
            workspace_id="ws-1",
            episode_id="ep-1",
            s_exec=0.8,
            s_llm=0.7,
            s_w=0.77,
        )
        eval_module.evaluate_all.return_value = {"ws-1": eval_result}

        selection_engine = MagicMock(spec=SelectionEngine)
        selection_engine.run_selection.return_value = ([], ["ws-1"])

        budget_allocator = MagicMock(spec=BudgetAllocator)
        budget_allocator.calculate_eta.return_value = {"ws-1": 0.008}

        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 9000

        scheduler = _make_scheduler(
            evaluation_module=eval_module,
            selection_engine=selection_engine,
            budget_allocator=budget_allocator,
            training_log=training_log,
        )

        result = scheduler.evaluate_and_select(patches={"ws-1": "diff"})

        assert isinstance(result, tuple)
        assert len(result) == 3
        evicted, survivors, eval_results = result
        assert isinstance(evicted, list)
        assert isinstance(survivors, list)
        assert isinstance(eval_results, dict)

    def test_get_workspaces(self):
        """get_workspaces() returns the workspace list from WorkspaceManager."""
        ws_1 = MagicMock(spec=Workspace)
        ws_1.workspace_id = "ws-1"

        workspace_manager = MagicMock(spec=WorkspaceManager)
        workspace_manager.list_workspaces.return_value = [ws_1]

        scheduler = _make_scheduler(workspace_manager=workspace_manager)
        workspaces = scheduler.get_workspaces()

        assert len(workspaces) == 1
        workspace_manager.list_workspaces.assert_called_once()

    def test_create_workspaces(self):
        """create_workspaces() delegates to WorkspaceManager."""
        workspace_manager = MagicMock(spec=WorkspaceManager)
        scheduler = _make_scheduler(workspace_manager=workspace_manager)

        scheduler.create_workspaces()

        workspace_manager.create.assert_called()

    def test_replace_evicted(self):
        """replace_evicted() delegates to WorkspaceManager."""
        workspace_manager = MagicMock(spec=WorkspaceManager)
        scheduler = _make_scheduler(workspace_manager=workspace_manager)

        # Seed evicted IDs so replace_evicted has workspaces to replace.
        scheduler._evicted_ids = ["ws-old-1", "ws-old-2"]

        new_configs = [{"name": "ws-new-1"}, {"name": "ws-new-2"}]
        scheduler.replace_evicted(new_configs)

        workspace_manager.replace.assert_called()

    def test_get_metered_llm_callback(self):
        """get_metered_llm_callback() returns a callable that goes through ResourceMeter."""
        resource_meter = MagicMock(spec=ResourceMeter)
        resource_meter.process.return_value = _make_response()
        scheduler = _make_scheduler(resource_meter=resource_meter)

        callback = scheduler.get_metered_llm_callback(
            workspace_id="ws-1",
            agent_id="agent-1",
        )

        assert callable(callback)
        request = _make_request()
        result = callback(request)
        assert isinstance(result, LLMResponse)
        resource_meter.process.assert_called_once()

    def test_get_system_llm_callback(self):
        """get_system_llm_callback() returns a callable that goes through SystemLLM."""
        system_llm = MagicMock(spec=SystemLLM)
        system_llm.call.return_value = _make_response()
        scheduler = _make_scheduler(system_llm=system_llm)

        callback = scheduler.get_system_llm_callback()

        assert callable(callback)
        request = _make_request()
        result = callback(request)
        assert isinstance(result, LLMResponse)
        system_llm.call.assert_called_once()

    def test_get_balance(self):
        """get_balance() delegates to TrainingLog.get_balance."""
        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 4200
        scheduler = _make_scheduler(training_log=training_log)

        balance = scheduler.get_balance("ws-1")

        assert balance == 4200
        training_log.get_balance.assert_called_once_with("ws-1")

    def test_evaluate_and_select_computes_eta(self):
        """evaluate_and_select() calls BudgetAllocator.calculate_eta with scores and costs."""
        eval_module = MagicMock(spec=EvaluationModule)
        eval_result_1 = EvalResult(
            workspace_id="ws-1", episode_id="ep-1",
            s_exec=0.8, s_llm=0.7, s_w=0.77,
        )
        eval_result_2 = EvalResult(
            workspace_id="ws-2", episode_id="ep-1",
            s_exec=0.3, s_llm=0.4, s_w=0.33,
        )
        eval_module.evaluate_all.return_value = {
            "ws-1": eval_result_1,
            "ws-2": eval_result_2,
        }

        budget_allocator = MagicMock(spec=BudgetAllocator)
        budget_allocator.calculate_eta.return_value = {
            "ws-1": 0.008,
            "ws-2": 0.002,
        }

        selection_engine = MagicMock(spec=SelectionEngine)
        selection_engine.run_selection.return_value = (["ws-2"], ["ws-1"])

        training_log = MagicMock(spec=TrainingLog)
        training_log.get_balance.return_value = 9000

        scheduler = _make_scheduler(
            evaluation_module=eval_module,
            budget_allocator=budget_allocator,
            selection_engine=selection_engine,
            training_log=training_log,
        )

        scheduler.evaluate_and_select(patches={"ws-1": "diff1", "ws-2": "diff2"})

        budget_allocator.calculate_eta.assert_called_once()
        # Verify scores were passed
        call_args = budget_allocator.calculate_eta.call_args
        scores = call_args[0][0] if call_args[0] else call_args[1]["workspace_scores"]
        assert "ws-1" in scores
        assert "ws-2" in scores
