"""Training entry point — episode loop orchestration."""
from midas_agent.config import MidasConfig
from midas_agent.evaluation.criteria_cache import CriteriaCache
from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.evaluation.llm_judge import LLMJudge
from midas_agent.evaluation.module import EvaluationModule
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.budget_allocator import AdaptiveMultiplier, BudgetAllocator
from midas_agent.scheduler.resource_meter import ResourceMeter
from midas_agent.scheduler.scheduler import Scheduler
from midas_agent.scheduler.selection import SelectionEngine
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.system_llm import SystemLLM
from midas_agent.scheduler.training_log import HookSet, TrainingLog
from midas_agent.scheduler.storage import InMemoryStorageBackend
from midas_agent.workspace.manager import WorkspaceManager


class _StubLLMProvider(LLMProvider):
    """Minimal LLM provider for offline/test usage."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            tool_calls=None,
            usage=TokenUsage(input_tokens=0, output_tokens=0),
        )


def run_training(config: MidasConfig) -> None:
    """Run the full training loop for one or more episodes.

    Phases per episode:
    1. Create workspaces (first episode only)
    2. Allocate budgets
    3. Execute agent work
    4. Collect patches / submit_patch
    5. Evaluate and select
    6. Replace evicted workspaces
    """
    # -- Wire up all components --
    storage = InMemoryStorageBackend()
    hooks = HookSet()
    serial_queue = SerialQueue()
    training_log = TrainingLog(
        storage=storage, hooks=hooks, serial_queue=serial_queue,
    )

    llm_provider = _StubLLMProvider()
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
            lambda req: resource_meter.process(req, entity_id=ws_id)
        ),
        system_llm_callback=lambda req: system_llm.call(req),
    )

    execution_scorer = ExecutionScorer(docker_image="", timeout=300)
    criteria_cache = CriteriaCache(cache_dir="/tmp/midas_criteria")
    llm_judge = LLMJudge(
        llm_provider=llm_provider, criteria_cache=criteria_cache,
    )
    evaluation_module = EvaluationModule(
        execution_scorer=execution_scorer,
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

    # -- Episode loop --
    scheduler.create_workspaces()
    scheduler.allocate_budgets()
