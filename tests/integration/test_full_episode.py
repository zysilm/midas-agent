"""Integration Test Suite 9: Full Episode Lifecycle (End-to-End).

TDD red phase: all tests should FAIL because the production stubs
raise NotImplementedError. These tests define expected behavior for the
complete training episode lifecycle.

Components under test:
  - run_training() (entry point)
  - Scheduler (full assembly orchestrating all sub-components)
  - BudgetAllocator, SelectionEngine, ResourceMeter
  - TrainingLog, SerialQueue, InMemoryStorageBackend
  - EvaluationModule, LLMJudge, CriteriaCache
  - WorkspaceManager, StubWorkspace
  - Observer (via SpyHookSet)

Mocked:
  - LLMProvider      -> FakeLLMProvider (conftest)
  - ExecutionScorer  -> FakeExecutionScorer (conftest)
  - Workspace        -> StubWorkspace (conftest)
"""
from __future__ import annotations

import json
import os
import time

import pytest

from midas_agent.config import MidasConfig
from midas_agent.evaluation.criteria_cache import CriteriaCache
from midas_agent.evaluation.llm_judge import LLMJudge
from midas_agent.evaluation.module import EvalResult, EvaluationModule
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.budget_allocator import AdaptiveMultiplier, BudgetAllocator
from midas_agent.scheduler.resource_meter import BudgetExhaustedError, ResourceMeter
from midas_agent.scheduler.scheduler import Scheduler
from midas_agent.scheduler.selection import SelectionEngine
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.scheduler.training_log import HookSet, TrainingLog
from midas_agent.workspace.manager import WorkspaceManager

from tests.integration.conftest import (
    FAKE_ISSUE,
    FakeExecutionScorer,
    FakeLLMProvider,
    InMemoryStorageBackend,
    SpyHookSet,
    StubWorkspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(content: str = "ok") -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=None,
        usage=TokenUsage(input_tokens=50, output_tokens=50),
    )


def _criteria_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(["correctness", "completeness", "style"]),
        tool_calls=None,
        usage=TokenUsage(input_tokens=20, output_tokens=30),
    )


def _eval_response(score: float = 0.7) -> LLMResponse:
    return LLMResponse(
        content=str(score),
        tool_calls=None,
        usage=TokenUsage(input_tokens=15, output_tokens=5),
    )


def _build_scheduler(
    config: MidasConfig,
    exec_scores: dict[str, float],
    llm_responses: list[LLMResponse] | None = None,
    temp_dir: str = "/tmp/midas_test_episode",
) -> tuple[
    Scheduler,
    TrainingLog,
    SpyHookSet,
    FakeLLMProvider,
    FakeExecutionScorer,
    InMemoryStorageBackend,
    WorkspaceManager,
]:
    """Build a complete Scheduler with all sub-components wired together."""
    storage = InMemoryStorageBackend()
    hooks = SpyHookSet()
    queue = SerialQueue()
    training_log = TrainingLog(storage=storage, hooks=hooks, serial_queue=queue)

    responses = llm_responses or [_make_llm_response()] * 50
    fake_llm = FakeLLMProvider(responses=responses)
    fake_scorer = FakeExecutionScorer(scores=exec_scores)

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

    def call_llm_factory(workspace_id: str):
        return lambda req: resource_meter.process(req, entity_id=workspace_id)

    system_llm_callback = lambda req: system_llm.call(req)

    workspace_manager = WorkspaceManager(
        config=config,
        call_llm_factory=call_llm_factory,
        system_llm_callback=system_llm_callback,
    )

    cache_dir = os.path.join(temp_dir, "criteria_cache")
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

    return (
        scheduler,
        training_log,
        hooks,
        fake_llm,
        fake_scorer,
        storage,
        workspace_manager,
    )


# ===========================================================================
# Integration tests
# ===========================================================================


@pytest.mark.integration
class TestFullEpisodeLifecycle:
    """Suite 9: Full Episode Lifecycle (End-to-End)."""

    # -----------------------------------------------------------------------
    # IT-9.1: Single episode -- Config Evolution, 3 workspaces
    # -----------------------------------------------------------------------

    def test_single_episode_config_evolution(
        self, config_evolution_config, temp_dir, fake_issue
    ):
        """Single episode with 3 workspaces in Config Evolution mode.

        Expected flow:
        1. create_workspaces() creates 3 workspaces
        2. allocate_budgets() produces 3 allocate records
        3. All workspaces execute and produce patches
        4. evaluate_and_select() produces 3 EvalResults
        5. 1 workspace evicted (n_evict=1)
        6. 1 replacement workspace created
        7. Final workspace count = 3
        """
        config = config_evolution_config
        exec_scores = {"ws-0": 0.8, "ws-1": 0.5, "ws-2": 0.2}

        # Provide enough LLM responses for the full pipeline
        llm_responses = [_make_llm_response()] * 20 + [
            _criteria_response(), _eval_response(0.6),
            _eval_response(0.5), _eval_response(0.3),
        ] + [_make_llm_response()] * 20

        scheduler, training_log, hooks, fake_llm, fake_scorer, storage, ws_mgr = (
            _build_scheduler(
                config=config,
                exec_scores=exec_scores,
                llm_responses=llm_responses,
                temp_dir=temp_dir,
            )
        )

        # Step 1: Create workspaces
        scheduler.create_workspaces()
        workspaces = scheduler.get_workspaces()
        assert len(workspaces) == 3

        # Step 2: Allocate budgets
        scheduler.allocate_budgets()

        # Verify 3 allocate log entries
        alloc_entries = training_log.get_log_entries(LogFilter(type="allocate"))
        assert len(alloc_entries) == 3, (
            f"Expected 3 allocate entries, got {len(alloc_entries)}"
        )

        # Verify allocate hooks fired 3 times
        hooks.assert_called("on_allocate", times=3)

        # Step 3: Simulate execution -- each workspace produces a patch
        patches = {}
        for ws in workspaces:
            ws.execute(fake_issue)
            ws.submit_patch()
            patches[ws.workspace_id] = f"patch content for {ws.workspace_id}"

        # Step 4: Evaluate and select
        evicted, survivors, eval_results = scheduler.evaluate_and_select(patches)

        # 3 EvalResults
        assert len(eval_results) == 3
        for ws_id, result in eval_results.items():
            assert isinstance(result, EvalResult)
            assert result.workspace_id == ws_id

        # Step 5: 1 workspace evicted
        assert len(evicted) == 1, f"Expected 1 evicted, got {len(evicted)}"
        assert len(survivors) == 2

        # Step 6: Replace evicted workspace
        new_configs = [{"variant": "mutated"}]
        scheduler.replace_evicted(new_configs)

        # Step 7: Final count = 3
        final_workspaces = scheduler.get_workspaces()
        assert len(final_workspaces) == 3

    # -----------------------------------------------------------------------
    # IT-9.2: Two consecutive episodes -- eta carry-forward
    # -----------------------------------------------------------------------

    def test_two_episodes_eta_carry_forward(
        self, config_evolution_config, temp_dir, fake_issue
    ):
        """Two consecutive episodes. The second episode uses etas computed
        from the first. A new workspace (replacement) gets median eta.
        The adaptive multiplier updates between episodes."""
        config = config_evolution_config
        exec_scores = {"ws-0": 0.9, "ws-1": 0.4, "ws-2": 0.1}

        llm_responses = [_make_llm_response()] * 100

        scheduler, training_log, hooks, fake_llm, fake_scorer, storage, ws_mgr = (
            _build_scheduler(
                config=config,
                exec_scores=exec_scores,
                llm_responses=llm_responses,
                temp_dir=temp_dir,
            )
        )

        # ===== Episode 1 =====
        scheduler.create_workspaces()
        scheduler.allocate_budgets()

        workspaces_ep1 = scheduler.get_workspaces()
        assert len(workspaces_ep1) == 3

        patches_ep1 = {}
        for ws in workspaces_ep1:
            ws.execute(fake_issue)
            ws.submit_patch()
            patches_ep1[ws.workspace_id] = f"patch content for {ws.workspace_id}"

        evicted_ep1, survivors_ep1, results_ep1 = scheduler.evaluate_and_select(
            patches_ep1
        )
        assert len(evicted_ep1) == 1
        assert len(survivors_ep1) == 2
        assert len(results_ep1) == 3

        # Notify workspaces of episode results
        for ws in workspaces_ep1:
            ws.post_episode(results_ep1, evicted_ids=evicted_ep1)

        # Replace evicted
        scheduler.replace_evicted([{"variant": "new_ep2"}])

        # ===== Episode 2 =====
        # Allocate again (should use etas from episode 1)
        scheduler.allocate_budgets()

        workspaces_ep2 = scheduler.get_workspaces()
        assert len(workspaces_ep2) == 3

        # The replacement workspace should have received a budget (median eta)
        for ws in workspaces_ep2:
            balance = scheduler.get_balance(ws.workspace_id)
            assert balance > 0, (
                f"Workspace {ws.workspace_id} must have positive budget in episode 2"
            )

        # Execute episode 2
        patches_ep2 = {}
        for ws in workspaces_ep2:
            ws.execute(fake_issue)
            ws.submit_patch()
            patches_ep2[ws.workspace_id] = f"patch content for {ws.workspace_id}"

        evicted_ep2, survivors_ep2, results_ep2 = scheduler.evaluate_and_select(
            patches_ep2
        )

        # Verify structural invariants across two episodes
        assert len(evicted_ep2) == 1
        assert len(survivors_ep2) == 2
        assert len(results_ep2) == 3

        # There should be allocate records for both episodes (3 + 3 = 6)
        alloc_entries = training_log.get_log_entries(LogFilter(type="allocate"))
        assert len(alloc_entries) == 6

    # -----------------------------------------------------------------------
    # IT-9.3: Single episode -- Graph Emergence, 2 workspaces, no eviction
    # -----------------------------------------------------------------------

    def test_single_episode_graph_emergence_no_eviction(
        self, graph_emergence_config, temp_dir, fake_issue
    ):
        """Graph Emergence mode with 2 workspaces. No eviction should occur.
        All consume entries must carry workspace_id for dual attribution."""
        config = graph_emergence_config
        exec_scores = {"ws-0": 0.6, "ws-1": 0.4}

        llm_responses = [_make_llm_response()] * 100

        scheduler, training_log, hooks, fake_llm, fake_scorer, storage, ws_mgr = (
            _build_scheduler(
                config=config,
                exec_scores=exec_scores,
                llm_responses=llm_responses,
                temp_dir=temp_dir,
            )
        )

        # Create and allocate
        scheduler.create_workspaces()
        scheduler.allocate_budgets()

        workspaces = scheduler.get_workspaces()
        assert len(workspaces) == 2

        # Execute
        patches = {}
        for ws in workspaces:
            ws.execute(fake_issue)
            ws.submit_patch()
            patches[ws.workspace_id] = f"patch content for {ws.workspace_id}"

        # Evaluate and select
        evicted, survivors, eval_results = scheduler.evaluate_and_select(patches)

        # Graph Emergence does NOT evict
        assert evicted == [], (
            f"Graph Emergence must not evict, but evicted: {evicted}"
        )
        assert len(survivors) == 2
        assert len(eval_results) == 2

        # Eviction hook must NOT have fired (from selection -- budget depletion
        # is a separate concern)
        # Note: we check that no eviction occurred from the selection process
        # The workspace count should remain 2
        final_workspaces = scheduler.get_workspaces()
        assert len(final_workspaces) == 2

        # All consume entries should have workspace_id set
        consume_entries = training_log.get_log_entries(LogFilter(type="consume"))
        for entry in consume_entries:
            assert entry.workspace_id is not None, (
                f"Graph Emergence consume entry {entry.tx_id} must carry workspace_id"
            )

    # -----------------------------------------------------------------------
    # IT-9.4: Budget exhaustion mid-episode
    # -----------------------------------------------------------------------

    def test_budget_exhaustion_mid_episode(self, temp_dir, fake_issue):
        """Low budget with high usage. BudgetExhaustedError fires during
        execute. on_workspace_evicted fires. S_exec=0 for the exhausted
        workspace. The workspace still participates in post_episode."""
        config = MidasConfig(
            initial_budget=200,  # Very low budget
            workspace_count=2,
            runtime_mode="config_evolution",
            n_evict=1,
            score_floor=0.01,
            multiplier_mode="static",
            multiplier_init=1.0,
            beta=0.3,
        )

        # One workspace will exhaust budget (score 0), the other succeeds
        exec_scores = {"ws-0": 0.0, "ws-1": 0.8}

        # First few calls succeed with high token usage, then one fails
        high_usage_response = LLMResponse(
            content="expensive response",
            tool_calls=None,
            usage=TokenUsage(input_tokens=150, output_tokens=150),  # 300 total
        )
        normal_response = _make_llm_response()
        llm_responses = [high_usage_response] + [normal_response] * 50

        scheduler, training_log, hooks, fake_llm, fake_scorer, storage, ws_mgr = (
            _build_scheduler(
                config=config,
                exec_scores=exec_scores,
                llm_responses=llm_responses,
                temp_dir=temp_dir,
            )
        )

        scheduler.create_workspaces()
        scheduler.allocate_budgets()

        workspaces = scheduler.get_workspaces()
        assert len(workspaces) == 2

        # Execute workspaces -- one may encounter BudgetExhaustedError
        patches = {}
        for ws in workspaces:
            try:
                ws.execute(fake_issue)
                ws.submit_patch()
                patches[ws.workspace_id] = f"patch content for {ws.workspace_id}"
            except BudgetExhaustedError:
                # Workspace ran out of budget mid-execution
                # It produces no patch (or a partial one scored as 0)
                patches[ws.workspace_id] = ""

        # on_workspace_evicted should have fired for the exhausted workspace
        eviction_calls = hooks.get_calls("on_workspace_evicted")
        assert len(eviction_calls) >= 1, (
            "on_workspace_evicted must fire when budget is exhausted"
        )

        # Evaluate -- the exhausted workspace should get S_exec=0
        evicted, survivors, eval_results = scheduler.evaluate_and_select(patches)

        # All workspaces should still get EvalResults
        assert len(eval_results) == 2

        # The exhausted workspace should have S_exec near 0
        exhausted_results = [
            r for r in eval_results.values()
            if r.s_exec == 0.0
        ]
        assert len(exhausted_results) >= 1, (
            "At least one workspace should have S_exec=0 due to budget exhaustion"
        )

        # Post-episode: all workspaces participate
        for ws in workspaces:
            ws.post_episode(eval_results, evicted_ids=evicted)

    # -----------------------------------------------------------------------
    # IT-9.5: Observer captures complete lifecycle via SpyHookSet
    # -----------------------------------------------------------------------

    def test_observer_captures_complete_lifecycle(
        self, config_evolution_config, temp_dir, fake_issue
    ):
        """SpyHookSet records the complete lifecycle:
        on_allocate (x3) -> on_consume (various) -> [on_workspace_evicted]
        -> post-episode operations.

        Verify that all expected hook categories are present and in order."""
        config = config_evolution_config
        exec_scores = {"ws-0": 0.9, "ws-1": 0.5, "ws-2": 0.1}

        llm_responses = [_make_llm_response()] * 100

        scheduler, training_log, hooks, fake_llm, fake_scorer, storage, ws_mgr = (
            _build_scheduler(
                config=config,
                exec_scores=exec_scores,
                llm_responses=llm_responses,
                temp_dir=temp_dir,
            )
        )

        # Full episode lifecycle
        scheduler.create_workspaces()
        scheduler.allocate_budgets()

        # Verify allocate hooks
        hooks.assert_called("on_allocate", times=3)

        workspaces = scheduler.get_workspaces()
        patches = {}
        for ws in workspaces:
            ws.execute(fake_issue)
            ws.submit_patch()
            patches[ws.workspace_id] = f"patch content for {ws.workspace_id}"

        # Any LLM calls during execution would have triggered on_consume hooks
        consume_calls_before_eval = hooks.get_calls("on_consume")

        evicted, survivors, eval_results = scheduler.evaluate_and_select(patches)

        # 1 workspace evicted in config_evolution (n_evict=1)
        assert len(evicted) == 1

        # Post-episode
        for ws in workspaces:
            ws.post_episode(eval_results, evicted_ids=evicted)

        # Verify the overall hook sequence:
        # 1. on_allocate should have been called for each workspace
        alloc_calls = hooks.get_calls("on_allocate")
        assert len(alloc_calls) == 3

        # 2. on_consume should have been called for LLM usage during execution
        consume_calls = hooks.get_calls("on_consume")
        # At minimum 0 (if workspaces did not use LLM in execute), but the
        # hook should have been correctly wired
        assert isinstance(consume_calls, list)

        # 3. The hook set should have recorded operations in chronological order
        # (allocates before consumes)
        all_alloc_timestamps = [c.get("timestamp", 0) for c in alloc_calls]
        if consume_calls:
            first_consume_ts = consume_calls[0].get("timestamp", float("inf"))
            # All allocations should precede the first consume
            for ts in all_alloc_timestamps:
                assert ts <= first_consume_ts, (
                    "Allocations must happen before consumption"
                )

    # -----------------------------------------------------------------------
    # IT-9.6: Phase ordering invariant
    # -----------------------------------------------------------------------

    def test_phase_ordering_invariant(
        self, config_evolution_config, temp_dir, fake_issue
    ):
        """Instrument StubWorkspace method calls with timestamps and verify
        that the episode phases execute in the correct order:

        receive_budget < execute < submit_patch < evaluate_and_select
        < post_episode < replace_evicted
        """
        config = config_evolution_config
        exec_scores = {"ws-0": 0.8, "ws-1": 0.5, "ws-2": 0.2}

        llm_responses = [_make_llm_response()] * 100

        scheduler, training_log, hooks, fake_llm, fake_scorer, storage, ws_mgr = (
            _build_scheduler(
                config=config,
                exec_scores=exec_scores,
                llm_responses=llm_responses,
                temp_dir=temp_dir,
            )
        )

        # Create workspaces
        scheduler.create_workspaces()

        # Phase timestamps tracker
        phase_timestamps: dict[str, float] = {}

        def record_phase(phase: str) -> None:
            phase_timestamps[phase] = time.monotonic()

        # Allocate budgets
        record_phase("allocate_budgets_start")
        scheduler.allocate_budgets()
        record_phase("allocate_budgets_end")

        # Execute all workspaces
        workspaces = scheduler.get_workspaces()

        record_phase("receive_budget_done")

        record_phase("execute_start")
        patches = {}
        for ws in workspaces:
            ws.execute(fake_issue)
        record_phase("execute_end")

        record_phase("submit_patch_start")
        for ws in workspaces:
            ws.submit_patch()
            patches[ws.workspace_id] = f"patch content for {ws.workspace_id}"
        record_phase("submit_patch_end")

        # Evaluate and select
        record_phase("evaluate_and_select_start")
        evicted, survivors, eval_results = scheduler.evaluate_and_select(patches)
        record_phase("evaluate_and_select_end")

        # Post-episode
        record_phase("post_episode_start")
        for ws in workspaces:
            ws.post_episode(eval_results, evicted_ids=evicted)
        record_phase("post_episode_end")

        # Replace evicted
        record_phase("replace_evicted_start")
        if evicted:
            scheduler.replace_evicted([{"variant": "new"}])
        record_phase("replace_evicted_end")

        # Assert phase ordering:
        # allocate < execute < submit_patch < evaluate_and_select
        # < post_episode < replace_evicted
        assert phase_timestamps["allocate_budgets_end"] <= phase_timestamps["execute_start"], (
            "allocate_budgets must complete before execute begins"
        )
        assert phase_timestamps["execute_end"] <= phase_timestamps["submit_patch_start"], (
            "execute must complete before submit_patch begins"
        )
        assert phase_timestamps["submit_patch_end"] <= phase_timestamps["evaluate_and_select_start"], (
            "submit_patch must complete before evaluate_and_select begins"
        )
        assert phase_timestamps["evaluate_and_select_end"] <= phase_timestamps["post_episode_start"], (
            "evaluate_and_select must complete before post_episode begins"
        )
        assert phase_timestamps["post_episode_end"] <= phase_timestamps["replace_evicted_start"], (
            "post_episode must complete before replace_evicted begins"
        )

        # Also verify using StubWorkspace call logs for the workspaces
        # that participated in the full lifecycle
        for ws in workspaces:
            call_names = [name for name, _ in ws.calls]
            # receive_budget must come before execute
            if "receive_budget" in call_names and "execute" in call_names:
                rb_idx = call_names.index("receive_budget")
                ex_idx = call_names.index("execute")
                assert rb_idx < ex_idx, (
                    f"receive_budget (idx={rb_idx}) must precede execute (idx={ex_idx}) "
                    f"for workspace {ws.workspace_id}"
                )
            # execute must come before submit_patch
            if "execute" in call_names and "submit_patch" in call_names:
                ex_idx = call_names.index("execute")
                sp_idx = call_names.index("submit_patch")
                assert ex_idx < sp_idx, (
                    f"execute (idx={ex_idx}) must precede submit_patch (idx={sp_idx}) "
                    f"for workspace {ws.workspace_id}"
                )
            # submit_patch must come before post_episode
            if "submit_patch" in call_names and "post_episode" in call_names:
                sp_idx = call_names.index("submit_patch")
                pe_idx = call_names.index("post_episode")
                assert sp_idx < pe_idx, (
                    f"submit_patch (idx={sp_idx}) must precede post_episode (idx={pe_idx}) "
                    f"for workspace {ws.workspace_id}"
                )
