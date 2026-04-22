"""Scheduler facade.

Orchestrates budget allocation, evaluation, selection, and workspace
lifecycle by delegating to injected sub-components.
"""
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from midas_agent.config import MidasConfig
from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.scheduler.budget_allocator import BudgetAllocator
from midas_agent.scheduler.resource_meter import ResourceMeter
from midas_agent.scheduler.selection import SelectionEngine
from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.scheduler.training_log import HookSet, TrainingLog

if TYPE_CHECKING:
    from midas_agent.evaluation.module import EvaluationModule, EvalResult
    from midas_agent.workspace.base import Workspace
    from midas_agent.workspace.manager import WorkspaceManager


class Scheduler:
    def __init__(
        self,
        config: MidasConfig,
        training_log: TrainingLog,
        resource_meter: ResourceMeter,
        system_llm: SystemLLM,
        budget_allocator: BudgetAllocator,
        selection_engine: SelectionEngine,
        workspace_manager: WorkspaceManager,
        evaluation_module: EvaluationModule,
        hooks: HookSet | None = None,
    ) -> None:
        self._config = config
        self._training_log = training_log
        self._resource_meter = resource_meter
        self._system_llm = system_llm
        self._budget_allocator = budget_allocator
        self._selection_engine = selection_engine
        self._workspace_manager = workspace_manager
        self._evaluation_module = evaluation_module

        self._episode_count: int = 0
        self._last_etas: dict[str, float] = {}
        self._last_total_consumption: int = 0
        self._evicted_ids: list[str] = []
        self._mid_episode_evictions: list[str] = []
        self._all_evicted_ever: set[str] = set()

        # Register on_workspace_evicted hook to collect mid-episode
        # budget-exhaustion evictions. Wrap any existing callback so
        # SpyHookSet (or other observers) still receive the event.
        # Resolve hooks: explicit parameter > TrainingLog's hooks > None.
        resolved_hooks = hooks or getattr(training_log, "_hooks", None)
        if resolved_hooks is not None:
            original_cb = resolved_hooks.on_workspace_evicted

            def _on_evicted(**kwargs: object) -> None:
                ws_id = kwargs.get("workspace_id")
                if ws_id is not None:
                    self._mid_episode_evictions.append(str(ws_id))
                    self._all_evicted_ever.add(str(ws_id))
                if original_cb is not None:
                    original_cb(**kwargs)

            resolved_hooks.on_workspace_evicted = _on_evicted

    # ------------------------------------------------------------------
    # Episode context
    # ------------------------------------------------------------------

    def set_current_issue(self, issue) -> None:
        """Set the current issue for this episode."""
        self._mid_episode_evictions = []
        self._evaluation_module.set_issue(issue)

    # ------------------------------------------------------------------
    # Budget allocation
    # ------------------------------------------------------------------

    def allocate_budgets(self) -> None:
        """Distribute token budgets to all active workspaces.

        Config evolution mode: every workspace gets ``initial_budget``
        every episode.  Budget resets each episode (no carry-over).
        """
        workspaces = self._workspace_manager.list_workspaces()
        for ws in workspaces:
            # Reset balance to initial_budget (not cumulative)
            current = self._training_log.get_balance(ws.workspace_id)
            if current != 0:
                # Zero out existing balance first
                self._training_log.record_consume(
                    entity_id=ws.workspace_id,
                    amount=current,
                    workspace_id=ws.workspace_id,
                )
            self._training_log.record_allocate(
                to=ws.workspace_id,
                amount=self._config.initial_budget,
            )
            ws._budget = self._config.initial_budget
            ws.budget_received = self._config.initial_budget

    # ------------------------------------------------------------------
    # Evaluation and selection
    # ------------------------------------------------------------------

    def evaluate_and_select(
        self,
        patches: dict[str, str],
    ) -> tuple[list[str], list[str], dict[str, EvalResult]]:
        """Evaluate workspace patches, compute etas, and run eviction.

        Returns:
            (evicted, survivors, eval_results) where *evicted* and
            *survivors* are lists of workspace IDs.
        """
        eval_results = self._evaluation_module.evaluate_all(patches)

        # Build per-workspace scores and costs.
        scores: dict[str, float] = {}
        costs: dict[str, int] = {}
        for ws_id, eval_result in eval_results.items():
            scores[ws_id] = eval_result.s_w
            # Cost = tokens consumed = initial_budget - current_balance.
            cost = self._config.initial_budget - self._training_log.get_balance(ws_id)
            costs[ws_id] = max(1, cost)

        etas = self._budget_allocator.calculate_eta(scores, costs)
        self._last_etas = etas
        self._last_total_consumption = sum(costs.values())

        evicted, survivors = self._selection_engine.run_selection(etas)
        self._evicted_ids = evicted

        # Compute eviction rate (ER) and update the adaptive multiplier.
        # Combine mid-episode budget-exhaustion evictions with ranking
        # evictions (deduplicated) per design doc 03-05.
        adaptive_mult = getattr(self._budget_allocator, "adaptive_multiplier", None)
        if adaptive_mult is not None:
            combined_evicted = set(self._mid_episode_evictions) | set(evicted)
            total_workspaces = len(etas)
            er = len(combined_evicted) / total_workspaces if total_workspaces > 0 else 0.0
            adaptive_mult.update(eviction_rate=er)

        return evicted, survivors, eval_results

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def get_workspaces(self) -> list[Workspace]:
        """Return all active workspaces."""
        return self._workspace_manager.list_workspaces()

    def create_workspaces(self) -> None:
        """Create the initial set of workspaces defined by config."""
        for i in range(self._config.workspace_count):
            self._workspace_manager.create(workspace_id=f"ws-{i}")

    def get_mid_episode_evictions(self) -> list[str]:
        """Return workspace IDs evicted mid-episode via budget exhaustion."""
        return list(self._mid_episode_evictions)

    def get_all_evictions(self) -> list[str]:
        """Return combined list: mid-episode evictions + SelectionEngine evictions."""
        seen: set[str] = set()
        combined: list[str] = []
        for ws_id in self._mid_episode_evictions + self._evicted_ids:
            if ws_id not in seen:
                seen.add(ws_id)
                combined.append(ws_id)
        return combined

    def get_all_evicted_ever(self) -> set[str]:
        """Return all workspace IDs evicted across all episodes."""
        return set(self._all_evicted_ever)

    def replace_evicted(self, new_configs: list[dict] | None = None) -> None:
        """Replace workspaces evicted by SelectionEngine (bottom-η).

        Mid-episode budget exhaustion is NOT eviction — the workspace
        still participates in evaluation and post-episode mutation.
        Only SelectionEngine evictions trigger replacement.
        """
        if not self._evicted_ids:
            return

        best_config = self._get_best_config()

        for i, old_id in enumerate(self._evicted_ids):
            new_id = f"ws-{self._episode_count}-new-{i}"
            self._workspace_manager.replace(old_id, new_id, best_config)

    def _get_best_config(self) -> dict | None:
        """Extract the workflow config from the highest-η workspace as a dict."""
        if not self._last_etas:
            return None

        best_ws_id = max(self._last_etas, key=self._last_etas.get)
        workspaces = self._workspace_manager.list_workspaces()

        for ws in workspaces:
            if ws.workspace_id == best_ws_id:
                wf_config = getattr(ws, "_workflow_config", None)
                if wf_config is None:
                    return None
                return {
                    "meta": {
                        "name": wf_config.meta.name,
                        "description": wf_config.meta.description,
                    },
                    "steps": [
                        {
                            "id": s.id,
                            "prompt": s.prompt,
                            "tools": s.tools,
                            "inputs": s.inputs,
                        }
                        for s in wf_config.steps
                    ],
                }

        return None

    # ------------------------------------------------------------------
    # LLM callback factories
    # ------------------------------------------------------------------

    def get_metered_llm_callback(
        self,
        workspace_id: str,
        agent_id: str | None = None,
    ) -> Callable[[LLMRequest], LLMResponse]:
        """Return a callback that routes LLM calls through ResourceMeter.

        Token consumption is attributed to *agent_id* (if provided) or
        *workspace_id*, and always tagged with *workspace_id*.
        """
        entity_id = agent_id if agent_id else workspace_id

        def _metered_callback(request: LLMRequest) -> LLMResponse:
            return self._resource_meter.process(
                request,
                entity_id=entity_id,
                workspace_id=workspace_id,
            )

        return _metered_callback

    def get_system_llm_callback(self) -> Callable[[LLMRequest], LLMResponse]:
        """Return an unmetered callback that calls the system LLM directly.

        No consume records are created in the TrainingLog.
        """
        def _system_callback(request: LLMRequest) -> LLMResponse:
            return self._system_llm.call(request)

        return _system_callback

    # ------------------------------------------------------------------
    # Balance query
    # ------------------------------------------------------------------

    def get_balance(self, entity_id: str) -> int:
        """Return the current token balance for *entity_id*."""
        return self._training_log.get_balance(entity_id)
