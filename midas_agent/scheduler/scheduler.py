"""Scheduler facade."""
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from midas_agent.config import MidasConfig
from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.scheduler.budget_allocator import BudgetAllocator
from midas_agent.scheduler.resource_meter import ResourceMeter
from midas_agent.scheduler.selection import SelectionEngine
from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.scheduler.training_log import TrainingLog

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
    ) -> None:
        raise NotImplementedError

    def allocate_budgets(self) -> None:
        raise NotImplementedError

    def evaluate_and_select(
        self,
        patches: dict[str, str],
    ) -> tuple[list[str], list[str], dict[str, EvalResult]]:
        raise NotImplementedError

    def get_workspaces(self) -> list[Workspace]:
        raise NotImplementedError

    def create_workspaces(self) -> None:
        raise NotImplementedError

    def replace_evicted(self, new_configs: list[dict]) -> None:
        raise NotImplementedError

    def get_metered_llm_callback(
        self,
        workspace_id: str,
        agent_id: str | None = None,
    ) -> Callable[[LLMRequest], LLMResponse]:
        raise NotImplementedError

    def get_system_llm_callback(self) -> Callable[[LLMRequest], LLMResponse]:
        raise NotImplementedError

    def get_balance(self, entity_id: str) -> int:
        raise NotImplementedError
