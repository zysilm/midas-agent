"""Integration Test Suite 4: Scheduler Facade.

TDD red phase -- all tests define expected behavior against NotImplementedError
production stubs. Tests use real sub-components wired together with
FakeLLMProvider and InMemoryStorageBackend from conftest.

Every test is marked @pytest.mark.integration.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from midas_agent.config import MidasConfig
from midas_agent.evaluation.module import EvaluationModule, EvalResult
from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.budget_allocator import AdaptiveMultiplier, BudgetAllocator
from midas_agent.scheduler.resource_meter import ResourceMeter
from midas_agent.scheduler.scheduler import Scheduler
from midas_agent.scheduler.selection import SelectionEngine
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.scheduler.training_log import TrainingLog, HookSet
from midas_agent.workspace.manager import WorkspaceManager

from tests.integration.conftest import (
    FakeLLMProvider,
    InMemoryStorageBackend,
    SpyHookSet,
    StubWorkspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_RESPONSE = LLMResponse(
    content="fake response",
    tool_calls=None,
    usage=TokenUsage(input_tokens=10, output_tokens=5),
)


def _make_request(content: str = "test") -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": content}], model="m")


def _build_scheduler(
    config: MidasConfig,
    storage: InMemoryStorageBackend,
    hooks: SpyHookSet,
    llm_provider: FakeLLMProvider,
) -> Scheduler:
    """Assemble a full Scheduler with real sub-components.

    Uses real TrainingLog, ResourceMeter, SystemLLM, BudgetAllocator,
    SelectionEngine, WorkspaceManager, and EvaluationModule -- all backed
    by FakeLLMProvider and InMemoryStorageBackend so no external services
    are needed.
    """
    serial_queue = SerialQueue()
    training_log = TrainingLog(
        storage=storage,
        hooks=hooks,
        serial_queue=serial_queue,
    )
    resource_meter = ResourceMeter(
        training_log=training_log,
        llm_provider=llm_provider,
    )
    system_llm = SystemLLM(llm_provider=llm_provider)

    adaptive_multiplier = AdaptiveMultiplier(
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
        adaptive_multiplier=adaptive_multiplier,
    )
    selection_engine = SelectionEngine(
        runtime_mode=config.runtime_mode,
        n_evict=config.n_evict,
    )

    # Scheduler builds metered callbacks via get_metered_llm_callback, and
    # WorkspaceManager needs a factory that creates per-workspace callbacks.
    # We pass lambdas that will be provided by the Scheduler once assembled.
    # For WorkspaceManager construction we use placeholder callbacks that
    # the Scheduler will override internally.
    workspace_manager = WorkspaceManager(
        config=config,
        call_llm_factory=lambda ws_id: (lambda req: resource_meter.process(req, ws_id, ws_id)),
        system_llm_callback=lambda req: system_llm.call(req),
    )

    # EvaluationModule requires ExecutionScorer and LLMJudge.
    # For integration tests we use MagicMock stubs that the individual tests
    # will configure as needed.
    evaluation_module = MagicMock(spec=EvaluationModule)

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
    return scheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage() -> InMemoryStorageBackend:
    return InMemoryStorageBackend()


@pytest.fixture
def hooks() -> SpyHookSet:
    return SpyHookSet()


@pytest.fixture
def llm_provider() -> FakeLLMProvider:
    return FakeLLMProvider(responses=[_DEFAULT_RESPONSE])


@pytest.fixture
def scheduler_assembly(config_evolution_config, storage, hooks, llm_provider):
    """Full Scheduler assembly with real sub-components.

    Returns a dict with every component for fine-grained assertion.
    """
    config = config_evolution_config
    serial_queue = SerialQueue()
    training_log = TrainingLog(
        storage=storage,
        hooks=hooks,
        serial_queue=serial_queue,
    )
    resource_meter = ResourceMeter(
        training_log=training_log,
        llm_provider=llm_provider,
    )
    system_llm = SystemLLM(llm_provider=llm_provider)

    adaptive_multiplier = AdaptiveMultiplier(
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
        adaptive_multiplier=adaptive_multiplier,
    )
    selection_engine = SelectionEngine(
        runtime_mode=config.runtime_mode,
        n_evict=config.n_evict,
    )
    workspace_manager = WorkspaceManager(
        config=config,
        call_llm_factory=lambda ws_id: (lambda req: resource_meter.process(req, ws_id, ws_id)),
        system_llm_callback=lambda req: system_llm.call(req),
    )

    evaluation_module = MagicMock(spec=EvaluationModule)

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

    return {
        "scheduler": scheduler,
        "config": config,
        "training_log": training_log,
        "resource_meter": resource_meter,
        "system_llm": system_llm,
        "budget_allocator": budget_allocator,
        "selection_engine": selection_engine,
        "workspace_manager": workspace_manager,
        "evaluation_module": evaluation_module,
        "storage": storage,
        "hooks": hooks,
        "llm_provider": llm_provider,
    }


# ---------------------------------------------------------------------------
# IT-4.1: allocate_budgets() cold start
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAllocateBudgetsColdStart:
    """IT-4.1: On the first episode (cold start), every workspace receives
    initial_budget tokens and the TrainingLog records one allocate entry per
    workspace.
    """

    def test_each_workspace_receives_initial_budget(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        training_log = scheduler_assembly["training_log"]
        config = scheduler_assembly["config"]

        # Create the 3 workspaces expected by config_evolution_config.
        sch.create_workspaces()
        workspaces = sch.get_workspaces()
        assert len(workspaces) == config.workspace_count

        # Cold start allocation.
        sch.allocate_budgets()

        # Each workspace should have received initial_budget via receive_budget().
        for ws in workspaces:
            balance = training_log.get_balance(ws.workspace_id)
            assert balance == config.initial_budget, (
                f"Workspace {ws.workspace_id} expected balance "
                f"{config.initial_budget}, got {balance}"
            )

    def test_training_log_has_allocate_records(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        storage = scheduler_assembly["storage"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        # Filter for allocate-type entries.
        allocate_entries = [e for e in storage.entries if e.type == "allocate"]
        assert len(allocate_entries) == config.workspace_count

        for entry in allocate_entries:
            assert entry.amount == config.initial_budget

    def test_workspace_receive_budget_called(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        # If workspaces are StubWorkspace, we can verify calls.
        # With real WorkspaceManager the workspaces may not be stubs,
        # but the balance in TrainingLog is the canonical record.
        for ws in workspaces:
            balance = scheduler_assembly["training_log"].get_balance(ws.workspace_id)
            assert balance == config.initial_budget


# ---------------------------------------------------------------------------
# IT-4.2: allocate_budgets() subsequent episode
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAllocateBudgetsSubsequentEpisode:
    """IT-4.2: After a first episode with known scores, the second allocation
    distributes budget proportional to etas.
    """

    def test_allocations_proportional_to_etas(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        training_log = scheduler_assembly["training_log"]
        evaluation_module = scheduler_assembly["evaluation_module"]
        config = scheduler_assembly["config"]

        # Create workspaces and do cold-start allocation.
        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]

        # Simulate consumption so workspaces have non-zero costs.
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=500, workspace_id=ws_id)

        # Simulate an evaluation phase: provide known scores.
        eval_results = {}
        scores = [0.8, 0.5, 0.1]
        for ws_id, score in zip(ws_ids, scores):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id,
                episode_id="ep-1",
                s_exec=score,
                s_llm=score,
                s_w=score,
            )
        evaluation_module.evaluate_all.return_value = eval_results

        patches = {ws_id: f"patch-{ws_id}" for ws_id in ws_ids}
        sch.evaluate_and_select(patches=patches)

        # Record balances before second allocation.
        balances_before = {ws_id: training_log.get_balance(ws_id) for ws_id in ws_ids}

        # Second allocation should be proportional to etas.
        sch.allocate_budgets()

        balances_after = {ws_id: training_log.get_balance(ws_id) for ws_id in ws_ids}
        deltas = {
            ws_id: balances_after[ws_id] - balances_before[ws_id]
            for ws_id in ws_ids
        }

        # The workspace with the highest score should receive the largest allocation.
        allocations = list(deltas.values())
        # ws_ids[0] had score 0.8 => highest eta => largest allocation.
        assert deltas[ws_ids[0]] >= deltas[ws_ids[1]], (
            f"Higher-scoring workspace should get more budget: "
            f"{deltas[ws_ids[0]]} vs {deltas[ws_ids[1]]}"
        )
        assert deltas[ws_ids[1]] >= deltas[ws_ids[2]], (
            f"Medium-scoring workspace should get more than lowest: "
            f"{deltas[ws_ids[1]]} vs {deltas[ws_ids[2]]}"
        )

        # Total allocated should be positive.
        assert sum(allocations) > 0


# ---------------------------------------------------------------------------
# IT-4.3: evaluate_and_select()
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEvaluateAndSelect:
    """IT-4.3: evaluate_and_select() evaluates patches, computes etas,
    performs eviction, and returns (evicted, survivors, eval_results).
    """

    def test_returns_correct_structure(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        evaluation_module = scheduler_assembly["evaluation_module"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]

        # Simulate consumption.
        training_log = scheduler_assembly["training_log"]
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=300, workspace_id=ws_id)

        # Configure fake evaluation results.
        eval_results = {}
        scores = [0.9, 0.5, 0.05]
        for ws_id, score in zip(ws_ids, scores):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id,
                episode_id="ep-1",
                s_exec=score,
                s_llm=score,
                s_w=score,
            )
        evaluation_module.evaluate_all.return_value = eval_results

        patches = {ws_id: f"diff-{ws_id}" for ws_id in ws_ids}
        evicted, survivors, results = sch.evaluate_and_select(patches=patches)

        # Return types.
        assert isinstance(evicted, list)
        assert isinstance(survivors, list)
        assert isinstance(results, dict)

        # eval_results should contain all workspace ids.
        for ws_id in ws_ids:
            assert ws_id in results

    def test_eviction_applied(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        evaluation_module = scheduler_assembly["evaluation_module"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]

        training_log = scheduler_assembly["training_log"]
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=200, workspace_id=ws_id)

        # Workspace with lowest score should be evicted (n_evict=1).
        eval_results = {}
        scores = [0.9, 0.6, 0.02]
        for ws_id, score in zip(ws_ids, scores):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id,
                episode_id="ep-1",
                s_exec=score,
                s_llm=score,
                s_w=score,
            )
        evaluation_module.evaluate_all.return_value = eval_results

        patches = {ws_id: f"diff-{ws_id}" for ws_id in ws_ids}
        evicted, survivors, _ = sch.evaluate_and_select(patches=patches)

        # n_evict=1, so exactly 1 evicted, 2 survivors.
        assert len(evicted) == config.n_evict
        assert len(survivors) == config.workspace_count - config.n_evict

        # The worst performer should be evicted.
        assert ws_ids[2] in evicted

    def test_etas_computed(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        evaluation_module = scheduler_assembly["evaluation_module"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]

        training_log = scheduler_assembly["training_log"]
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=100, workspace_id=ws_id)

        eval_results = {}
        scores = [0.7, 0.4, 0.1]
        for ws_id, score in zip(ws_ids, scores):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id,
                episode_id="ep-1",
                s_exec=score,
                s_llm=score,
                s_w=score,
            )
        evaluation_module.evaluate_all.return_value = eval_results

        patches = {ws_id: f"diff-{ws_id}" for ws_id in ws_ids}
        evicted, survivors, results = sch.evaluate_and_select(patches=patches)

        # evaluate_all was invoked with the patches.
        evaluation_module.evaluate_all.assert_called_once_with(patches)

        # Results came back with the expected eval data.
        for ws_id in ws_ids:
            assert results[ws_id].workspace_id == ws_id


# ---------------------------------------------------------------------------
# IT-4.4: get_metered_llm_callback() produces working callback
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMeteredLLMCallback:
    """IT-4.4: The metered callback routes through ResourceMeter and records
    a consume entry in TrainingLog with the correct workspace_id.
    """

    def test_callback_invokes_resource_meter(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        training_log = scheduler_assembly["training_log"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws = workspaces[0]

        callback = sch.get_metered_llm_callback(workspace_id=ws.workspace_id)
        assert callable(callback)

        request = _make_request("metered call")
        response = callback(request)

        # Should return a valid LLMResponse.
        assert isinstance(response, LLMResponse)

    def test_consume_record_has_correct_workspace_id(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        storage = scheduler_assembly["storage"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws = workspaces[0]

        callback = sch.get_metered_llm_callback(workspace_id=ws.workspace_id)
        callback(_make_request("metered call"))

        # There should be a consume entry attributed to the correct workspace.
        consume_entries = [
            e for e in storage.entries
            if e.type == "consume" and e.workspace_id == ws.workspace_id
        ]
        assert len(consume_entries) >= 1, (
            f"Expected at least one consume entry for {ws.workspace_id}, "
            f"found {len(consume_entries)}"
        )
        # The consume entry should have a positive amount.
        for entry in consume_entries:
            assert entry.amount > 0

    def test_callback_with_agent_id(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        storage = scheduler_assembly["storage"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws = workspaces[0]

        callback = sch.get_metered_llm_callback(
            workspace_id=ws.workspace_id,
            agent_id="agent-alpha",
        )
        assert callable(callback)

        response = callback(_make_request("agent call"))
        assert isinstance(response, LLMResponse)


# ---------------------------------------------------------------------------
# IT-4.5: get_system_llm_callback() produces unmetered callback
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSystemLLMCallback:
    """IT-4.5: The system LLM callback calls the underlying LLM provider
    but does NOT create any consume record in TrainingLog.
    """

    def test_callback_returns_response(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]

        callback = sch.get_system_llm_callback()
        assert callable(callback)

        response = callback(_make_request("system call"))
        assert isinstance(response, LLMResponse)

    def test_no_consume_record_in_training_log(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        storage = scheduler_assembly["storage"]

        # Record how many consume entries exist before the call.
        consume_before = len([e for e in storage.entries if e.type == "consume"])

        callback = sch.get_system_llm_callback()
        callback(_make_request("system call"))

        consume_after = len([e for e in storage.entries if e.type == "consume"])
        assert consume_after == consume_before, (
            "System LLM callback should NOT produce consume records, "
            f"but count went from {consume_before} to {consume_after}"
        )

    def test_llm_provider_actually_called(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        llm_provider = scheduler_assembly["llm_provider"]

        calls_before = llm_provider.call_count
        callback = sch.get_system_llm_callback()
        callback(_make_request("verify provider invoked"))

        assert llm_provider.call_count > calls_before, (
            "Expected FakeLLMProvider.complete() to be invoked by system LLM callback"
        )


# ---------------------------------------------------------------------------
# IT-4.6: replace_evicted() creates new workspaces
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReplaceEvicted:
    """IT-4.6: After eviction, replace_evicted() removes the old workspace
    and creates a new one.  Workspace count is preserved.
    """

    def test_old_workspace_removed_and_new_created(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        evaluation_module = scheduler_assembly["evaluation_module"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]
        original_count = len(ws_ids)

        # Simulate consumption and evaluation.
        training_log = scheduler_assembly["training_log"]
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=100, workspace_id=ws_id)

        eval_results = {}
        scores = [0.9, 0.6, 0.01]
        for ws_id, score in zip(ws_ids, scores):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id,
                episode_id="ep-1",
                s_exec=score,
                s_llm=score,
                s_w=score,
            )
        evaluation_module.evaluate_all.return_value = eval_results

        patches = {ws_id: f"diff-{ws_id}" for ws_id in ws_ids}
        evicted, survivors, _ = sch.evaluate_and_select(patches=patches)
        assert len(evicted) == 1

        evicted_id = evicted[0]

        # Replace the evicted workspace with a new config.
        new_configs = [{"name": f"replacement-for-{evicted_id}"}]
        sch.replace_evicted(new_configs)

        # Verify workspace count is preserved.
        updated_workspaces = sch.get_workspaces()
        assert len(updated_workspaces) == original_count

        # Old workspace should be gone.
        updated_ids = [ws.workspace_id for ws in updated_workspaces]
        assert evicted_id not in updated_ids

    def test_survivors_remain(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        evaluation_module = scheduler_assembly["evaluation_module"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]

        training_log = scheduler_assembly["training_log"]
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=100, workspace_id=ws_id)

        eval_results = {}
        scores = [0.8, 0.5, 0.02]
        for ws_id, score in zip(ws_ids, scores):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id,
                episode_id="ep-1",
                s_exec=score,
                s_llm=score,
                s_w=score,
            )
        evaluation_module.evaluate_all.return_value = eval_results

        patches = {ws_id: f"diff-{ws_id}" for ws_id in ws_ids}
        evicted, survivors, _ = sch.evaluate_and_select(patches=patches)

        new_configs = [{"name": "replacement"}]
        sch.replace_evicted(new_configs)

        updated_ids = [ws.workspace_id for ws in sch.get_workspaces()]
        for surv_id in survivors:
            assert surv_id in updated_ids, (
                f"Survivor {surv_id} should still be present after replacement"
            )


# ---------------------------------------------------------------------------
# IT-4.7: Callback identity isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCallbackIdentityIsolation:
    """IT-4.7: Metered callbacks for different workspaces produce consume
    records that are correctly attributed -- no cross-contamination.
    """

    def test_consume_records_attributed_correctly(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        storage = scheduler_assembly["storage"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        assert len(workspaces) >= 2
        ws1 = workspaces[0]
        ws2 = workspaces[1]

        cb1 = sch.get_metered_llm_callback(workspace_id=ws1.workspace_id)
        cb2 = sch.get_metered_llm_callback(workspace_id=ws2.workspace_id)

        # Call each callback.
        cb1(_make_request("call from ws1"))
        cb2(_make_request("call from ws2"))

        # Verify consume records are attributed to the correct workspace.
        consume_ws1 = [
            e for e in storage.entries
            if e.type == "consume" and e.workspace_id == ws1.workspace_id
        ]
        consume_ws2 = [
            e for e in storage.entries
            if e.type == "consume" and e.workspace_id == ws2.workspace_id
        ]

        assert len(consume_ws1) >= 1, (
            f"Expected consume entry for {ws1.workspace_id}"
        )
        assert len(consume_ws2) >= 1, (
            f"Expected consume entry for {ws2.workspace_id}"
        )

    def test_no_cross_contamination(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        storage = scheduler_assembly["storage"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws1 = workspaces[0]
        ws2 = workspaces[1]

        cb1 = sch.get_metered_llm_callback(workspace_id=ws1.workspace_id)
        cb2 = sch.get_metered_llm_callback(workspace_id=ws2.workspace_id)

        # Only call cb1.
        cb1(_make_request("only ws1"))

        # ws2 should have zero consume records from this call.
        consume_ws2_before = [
            e for e in storage.entries
            if e.type == "consume" and e.workspace_id == ws2.workspace_id
        ]
        assert len(consume_ws2_before) == 0, (
            "ws2 should have no consume records when only ws1 callback was invoked"
        )

        # Now call cb2.
        cb2(_make_request("only ws2"))

        # ws1 should still only have its original consume entry.
        consume_ws1 = [
            e for e in storage.entries
            if e.type == "consume" and e.workspace_id == ws1.workspace_id
        ]
        assert len(consume_ws1) == 1, (
            "ws1 should have exactly 1 consume record (not affected by ws2 call)"
        )


# ---------------------------------------------------------------------------
# IT-4.8: get_balance() delegates to TrainingLog
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGetBalance:
    """IT-4.8: get_balance() returns the same value as
    TrainingLog.get_balance() after operations.
    """

    def test_balance_matches_training_log(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        training_log = scheduler_assembly["training_log"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        for ws in workspaces:
            expected = training_log.get_balance(ws.workspace_id)
            actual = sch.get_balance(ws.workspace_id)
            assert actual == expected, (
                f"Scheduler.get_balance({ws.workspace_id}) = {actual}, "
                f"but TrainingLog.get_balance() = {expected}"
            )

    def test_balance_reflects_consumption(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        training_log = scheduler_assembly["training_log"]
        config = scheduler_assembly["config"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws = workspaces[0]

        # Consume some budget via metered callback.
        callback = sch.get_metered_llm_callback(workspace_id=ws.workspace_id)
        callback(_make_request("consume some budget"))

        # Balance should have decreased.
        balance = sch.get_balance(ws.workspace_id)
        assert balance < config.initial_budget, (
            f"Balance should be less than initial_budget after consumption, "
            f"got {balance}"
        )

        # Should still match TrainingLog.
        assert balance == training_log.get_balance(ws.workspace_id)

    def test_balance_after_multiple_operations(self, scheduler_assembly):
        sch = scheduler_assembly["scheduler"]
        training_log = scheduler_assembly["training_log"]

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws = workspaces[0]

        # Multiple consume operations.
        callback = sch.get_metered_llm_callback(workspace_id=ws.workspace_id)
        callback(_make_request("call 1"))
        callback(_make_request("call 2"))
        callback(_make_request("call 3"))

        balance = sch.get_balance(ws.workspace_id)
        expected = training_log.get_balance(ws.workspace_id)
        assert balance == expected, (
            f"After 3 metered calls, Scheduler.get_balance()={balance} "
            f"should equal TrainingLog.get_balance()={expected}"
        )


# ---------------------------------------------------------------------------
# IT-4.9: Adaptive multiplier updated after evaluate_and_select
# ---------------------------------------------------------------------------


def _build_adaptive_scheduler(
    config: MidasConfig,
    storage: InMemoryStorageBackend,
    hooks: SpyHookSet,
    llm_provider: FakeLLMProvider,
) -> dict:
    """Build Scheduler with adaptive multiplier, returning all components."""
    serial_queue = SerialQueue()
    training_log = TrainingLog(
        storage=storage, hooks=hooks, serial_queue=serial_queue,
    )
    resource_meter = ResourceMeter(
        training_log=training_log, llm_provider=llm_provider,
    )
    system_llm = SystemLLM(llm_provider=llm_provider)

    adaptive_multiplier = AdaptiveMultiplier(
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
        adaptive_multiplier=adaptive_multiplier,
    )
    selection_engine = SelectionEngine(
        runtime_mode=config.runtime_mode,
        n_evict=config.n_evict,
    )
    workspace_manager = WorkspaceManager(
        config=config,
        call_llm_factory=lambda ws_id: (
            lambda req: resource_meter.process(req, ws_id, ws_id)
        ),
        system_llm_callback=lambda req: system_llm.call(req),
    )
    evaluation_module = MagicMock(spec=EvaluationModule)

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

    return {
        "scheduler": scheduler,
        "config": config,
        "training_log": training_log,
        "adaptive_multiplier": adaptive_multiplier,
        "budget_allocator": budget_allocator,
        "evaluation_module": evaluation_module,
        "hooks": hooks,
    }


@pytest.mark.integration
class TestAdaptiveMultiplierLifecycle:
    """IT-4.9: Scheduler must call adaptive_multiplier.update() after
    evaluate_and_select with the correct eviction rate. The multiplier
    value must then affect the next episode's allocation pool.

    Design 03-05: 'Every evaluation triggers multiplier update with
    ER = evicted / total. M_pool = C_total_last_round × multiplier.'"""

    def test_multiplier_updated_after_config_evolution_eviction(self):
        """Config Evolution: 1 of 3 evicted → ER=1/3 → moderate inflate (×1.2).
        Multiplier must change from 1.0 to 1.2 after evaluate_and_select."""
        config = MidasConfig(
            initial_budget=10000,
            workspace_count=3,
            runtime_mode="config_evolution",
            n_evict=1,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
            beta=0.3,
        )
        assembly = _build_adaptive_scheduler(
            config, InMemoryStorageBackend(), SpyHookSet(),
            FakeLLMProvider(responses=[_DEFAULT_RESPONSE] * 50),
        )
        sch = assembly["scheduler"]
        am = assembly["adaptive_multiplier"]
        eval_mod = assembly["evaluation_module"]
        training_log = assembly["training_log"]

        assert am.current_value == 1.0

        sch.create_workspaces()
        sch.allocate_budgets()
        ws_ids = [ws.workspace_id for ws in sch.get_workspaces()]

        # Simulate consumption
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=500, workspace_id=ws_id)

        # Evaluation: scores differ so bottom ws gets evicted
        eval_results = {}
        for ws_id, score in zip(ws_ids, [0.9, 0.5, 0.1]):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id, episode_id="ep-1",
                s_exec=score, s_llm=score, s_w=score,
            )
        eval_mod.evaluate_all.return_value = eval_results

        patches = {ws_id: f"patch-{ws_id}" for ws_id in ws_ids}
        evicted, survivors, _ = sch.evaluate_and_select(patches)
        assert len(evicted) == 1

        # ER = 1/3 ≈ 0.33 → Zone 3 (moderate inflate) → ×1.2
        assert am.current_value == pytest.approx(1.2), (
            f"After ER=1/3, multiplier should be 1.2, got {am.current_value}"
        )

    def test_multiplier_deflates_when_no_eviction(self):
        """Config Evolution: 0 of 3 evicted → ER=0 → deflate (×0.95).
        n_evict=0 so no eviction happens."""
        config = MidasConfig(
            initial_budget=10000,
            workspace_count=3,
            runtime_mode="config_evolution",
            n_evict=0,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
            beta=0.3,
        )
        assembly = _build_adaptive_scheduler(
            config, InMemoryStorageBackend(), SpyHookSet(),
            FakeLLMProvider(responses=[_DEFAULT_RESPONSE] * 50),
        )
        sch = assembly["scheduler"]
        am = assembly["adaptive_multiplier"]
        eval_mod = assembly["evaluation_module"]
        training_log = assembly["training_log"]

        sch.create_workspaces()
        sch.allocate_budgets()
        ws_ids = [ws.workspace_id for ws in sch.get_workspaces()]

        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=500, workspace_id=ws_id)

        eval_results = {}
        for ws_id, score in zip(ws_ids, [0.9, 0.5, 0.1]):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id, episode_id="ep-1",
                s_exec=score, s_llm=score, s_w=score,
            )
        eval_mod.evaluate_all.return_value = eval_results

        patches = {ws_id: f"patch-{ws_id}" for ws_id in ws_ids}
        evicted, _, _ = sch.evaluate_and_select(patches)
        assert len(evicted) == 0

        # ER = 0 → Zone 1 → deflate by cool_down
        assert am.current_value == pytest.approx(1.0 * (1 - 0.05)), (
            f"After ER=0, multiplier should deflate to 0.95, got {am.current_value}"
        )

    def test_multiplier_affects_next_allocation_pool(self):
        """After multiplier inflates to 1.2, the second episode's total
        allocation = last_consumption × 1.2, not last_consumption × 1.0."""
        config = MidasConfig(
            initial_budget=10000,
            workspace_count=3,
            runtime_mode="config_evolution",
            n_evict=1,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
            beta=0.3,
        )
        assembly = _build_adaptive_scheduler(
            config, InMemoryStorageBackend(), SpyHookSet(),
            FakeLLMProvider(responses=[_DEFAULT_RESPONSE] * 100),
        )
        sch = assembly["scheduler"]
        am = assembly["adaptive_multiplier"]
        eval_mod = assembly["evaluation_module"]
        training_log = assembly["training_log"]

        # Episode 1: cold start
        sch.create_workspaces()
        sch.allocate_budgets()
        ws_ids = [ws.workspace_id for ws in sch.get_workspaces()]

        # Each consumes 3000 → total = 9000
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=3000, workspace_id=ws_id)

        eval_results = {}
        for ws_id, score in zip(ws_ids, [0.9, 0.5, 0.1]):
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id, episode_id="ep-1",
                s_exec=score, s_llm=score, s_w=score,
            )
        eval_mod.evaluate_all.return_value = eval_results

        patches = {ws_id: f"patch-{ws_id}" for ws_id in ws_ids}
        sch.evaluate_and_select(patches)

        # ER=1/3 → multiplier = 1.2
        assert am.current_value == pytest.approx(1.2)

        # Episode 2: allocate with inflated multiplier
        # Replace evicted workspace first
        sch.replace_evicted([{"name": "new"}])

        # Record balances before second allocation
        balances_before = {
            ws.workspace_id: training_log.get_balance(ws.workspace_id)
            for ws in sch.get_workspaces()
        }

        sch.allocate_budgets()

        balances_after = {
            ws.workspace_id: training_log.get_balance(ws.workspace_id)
            for ws in sch.get_workspaces()
        }

        # Total new allocation = sum of deltas
        total_new_alloc = sum(
            balances_after[ws_id] - balances_before.get(ws_id, 0)
            for ws_id in balances_after
        )

        # M_pool = last_total_consumption(9000) × multiplier(1.2) = 10800
        expected_pool = 9000 * 1.2
        assert pytest.approx(total_new_alloc, rel=0.05) == expected_pool, (
            f"Second episode allocation should be ~{expected_pool}, "
            f"got {total_new_alloc}"
        )


# ---------------------------------------------------------------------------
# IT-4.10: GE mode — mid-episode eviction feeds ER for adaptive multiplier
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGEModeAdaptiveMultiplier:
    """IT-4.10: In Graph Emergence mode, SelectionEngine returns evicted=[].
    But mid-episode budget exhaustion fires on_workspace_evicted. The
    Scheduler must include mid-episode evictions in the ER passed to
    adaptive_multiplier.update().

    Design: 'responsible agent budget exhausted → workspace evict (hard
    constraint)'. This is the only eviction signal in GE mode."""

    def test_ge_mid_episode_eviction_inflates_multiplier(self):
        """GE mode, 2 workspaces, ws-0 budget-exhausted mid-episode.
        ER = 1/2 = 0.5 → moderate inflate (×1.2). The multiplier must
        NOT stay at 1.0 just because SelectionEngine returns []."""
        config = MidasConfig(
            initial_budget=200,  # Very low to trigger exhaustion
            workspace_count=2,
            runtime_mode="graph_emergence",
            n_evict=1,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
            beta=0.3,
        )

        storage = InMemoryStorageBackend()
        hooks = SpyHookSet()

        # First call: 300 tokens → exhausts ws-0's 200 budget
        high_usage = LLMResponse(
            content="expensive",
            tool_calls=None,
            usage=TokenUsage(input_tokens=150, output_tokens=150),
        )
        normal = _DEFAULT_RESPONSE
        llm_provider = FakeLLMProvider(responses=[high_usage] + [normal] * 50)

        assembly = _build_adaptive_scheduler(config, storage, hooks, llm_provider)
        sch = assembly["scheduler"]
        am = assembly["adaptive_multiplier"]
        eval_mod = assembly["evaluation_module"]
        training_log = assembly["training_log"]

        assert am.current_value == 1.0

        sch.create_workspaces()
        sch.allocate_budgets()

        workspaces = sch.get_workspaces()
        ws_ids = [ws.workspace_id for ws in workspaces]

        # ws-0 exhausts budget (metered call consumes 300 > 200 budget)
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError
        for ws in workspaces:
            try:
                # Simulate a metered LLM call
                callback = sch.get_metered_llm_callback(ws.workspace_id)
                callback(_make_request("work"))
            except BudgetExhaustedError:
                pass

        # At least one workspace should have been evicted mid-episode
        mid_evictions = sch.get_mid_episode_evictions()
        assert len(mid_evictions) >= 1, (
            "Budget exhaustion must register as mid-episode eviction"
        )

        # Now evaluate — SelectionEngine returns evicted=[] for GE
        eval_results = {}
        for ws_id in ws_ids:
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id, episode_id="ep-1",
                s_exec=0.5, s_llm=0.5, s_w=0.5,
            )
        eval_mod.evaluate_all.return_value = eval_results

        patches = {ws_id: "" for ws_id in ws_ids}
        evicted, survivors, _ = sch.evaluate_and_select(patches)

        # SelectionEngine returns no eviction for GE
        assert evicted == []

        # But multiplier should have been updated with ER > 0
        # (based on mid-episode evictions)
        assert am.current_value > 1.0, (
            f"GE mode: mid-episode evictions should inflate multiplier, "
            f"but got {am.current_value}"
        )

    def test_ge_no_exhaustion_deflates_multiplier(self):
        """GE mode, 2 workspaces, neither exhausts budget. ER=0 → deflate."""
        config = MidasConfig(
            initial_budget=100000,  # High enough — no exhaustion
            workspace_count=2,
            runtime_mode="graph_emergence",
            n_evict=1,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
            beta=0.3,
        )

        assembly = _build_adaptive_scheduler(
            config, InMemoryStorageBackend(), SpyHookSet(),
            FakeLLMProvider(responses=[_DEFAULT_RESPONSE] * 50),
        )
        sch = assembly["scheduler"]
        am = assembly["adaptive_multiplier"]
        eval_mod = assembly["evaluation_module"]
        training_log = assembly["training_log"]

        sch.create_workspaces()
        sch.allocate_budgets()
        ws_ids = [ws.workspace_id for ws in sch.get_workspaces()]

        # Light consumption — no exhaustion
        for ws_id in ws_ids:
            training_log.record_consume(entity_id=ws_id, amount=100, workspace_id=ws_id)

        eval_results = {}
        for ws_id in ws_ids:
            eval_results[ws_id] = EvalResult(
                workspace_id=ws_id, episode_id="ep-1",
                s_exec=0.8, s_llm=0.8, s_w=0.8,
            )
        eval_mod.evaluate_all.return_value = eval_results

        patches = {ws_id: "patch" for ws_id in ws_ids}
        sch.evaluate_and_select(patches)

        # No mid-episode evictions, no ranking evictions → ER=0 → deflate
        assert am.current_value == pytest.approx(1.0 * (1 - 0.05)), (
            f"GE mode with no exhaustion: multiplier should deflate to 0.95, "
            f"got {am.current_value}"
        )

    def test_ge_all_exhausted_emergency_double(self):
        """GE mode, 2 workspaces, BOTH exhaust budget. ER=2/2=1.0 → ×1.5."""
        config = MidasConfig(
            initial_budget=100,  # Extremely low
            workspace_count=2,
            runtime_mode="graph_emergence",
            n_evict=1,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            er_target=0.1,
            cool_down=0.05,
            mult_min=0.5,
            mult_max=5.0,
            beta=0.3,
        )

        high_usage = LLMResponse(
            content="expensive",
            tool_calls=None,
            usage=TokenUsage(input_tokens=100, output_tokens=100),
        )
        assembly = _build_adaptive_scheduler(
            config, InMemoryStorageBackend(), SpyHookSet(),
            FakeLLMProvider(responses=[high_usage] * 50),
        )
        sch = assembly["scheduler"]
        am = assembly["adaptive_multiplier"]
        eval_mod = assembly["evaluation_module"]

        sch.create_workspaces()
        sch.allocate_budgets()
        ws_ids = [ws.workspace_id for ws in sch.get_workspaces()]

        # Both workspaces exhaust budget
        from midas_agent.scheduler.resource_meter import BudgetExhaustedError
        for ws in sch.get_workspaces():
            try:
                callback = sch.get_metered_llm_callback(ws.workspace_id)
                callback(_make_request("work"))
            except BudgetExhaustedError:
                pass

        # Both should be mid-episode evicted
        assert len(sch.get_mid_episode_evictions()) == 2

        eval_results = {
            ws_id: EvalResult(
                workspace_id=ws_id, episode_id="ep-1",
                s_exec=0.0, s_llm=0.0, s_w=0.0,
            )
            for ws_id in ws_ids
        }
        eval_mod.evaluate_all.return_value = eval_results
        sch.evaluate_and_select({ws_id: "" for ws_id in ws_ids})

        # ER = 2/2 = 1.0 → emergency inflate
        assert am.current_value == pytest.approx(1.5), (
            f"All workspaces exhausted: multiplier should inflate to 1.5, "
            f"got {am.current_value}"
        )
