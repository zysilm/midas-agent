"""Training entry point — episode loop orchestration."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

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
from midas_agent.types import Issue
from midas_agent.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


def _make_llm_provider(model: str, api_key: str, api_base: str) -> LLMProvider:
    """Create an LLM provider from config. Empty model = stub."""
    if not model:
        return _StubLLMProvider()
    from midas_agent.llm.litellm_provider import LiteLLMProvider
    return LiteLLMProvider(
        model=model,
        api_key=api_key or None,
        api_base=api_base or None,
    )


class _StubLLMProvider(LLMProvider):
    """Minimal LLM provider for offline/test usage."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            tool_calls=None,
            usage=TokenUsage(input_tokens=0, output_tokens=0),
        )


def load_swe_bench(split: str = "test") -> list[Issue]:
    """Load SWE-bench Verified from HuggingFace and map to Issue objects."""
    from datasets import load_dataset

    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split=split)
    issues: list[Issue] = []
    for row in ds:
        fail_to_pass = row.get("FAIL_TO_PASS", "[]")
        pass_to_pass = row.get("PASS_TO_PASS", "[]")
        if isinstance(fail_to_pass, str):
            fail_to_pass = json.loads(fail_to_pass)
        if isinstance(pass_to_pass, str):
            pass_to_pass = json.loads(pass_to_pass)

        issues.append(Issue(
            issue_id=row["instance_id"],
            repo=row["repo"],
            description=row["problem_statement"],
            base_commit=row.get("base_commit", ""),
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
        ))
    return issues


def clone_repo(repo: str, base_commit: str, dest: str) -> None:
    """Clone a GitHub repo at a specific commit into dest."""
    url = f"https://github.com/{repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", url, dest],
        check=True,
        capture_output=True,
    )
    if base_commit:
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", base_commit],
            cwd=dest,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", base_commit],
            cwd=dest,
            check=True,
            capture_output=True,
        )


def collect_patches(workspaces, patches_base_dir: str) -> dict[str, str]:
    """Read patch files written by submit_patch() from disk."""
    patches: dict[str, str] = {}
    for ws in workspaces:
        ws_patches_dir = os.path.join(patches_base_dir, ws.workspace_id)
        if not os.path.isdir(ws_patches_dir):
            patches[ws.workspace_id] = ""
            continue
        # Get the most recent patch file.
        patch_files = sorted(os.listdir(ws_patches_dir))
        if not patch_files:
            patches[ws.workspace_id] = ""
            continue
        latest = os.path.join(ws_patches_dir, patch_files[-1])
        with open(latest) as f:
            patches[ws.workspace_id] = f.read()
    return patches


def run_training(
    config: MidasConfig,
    issues: list[Issue] | None = None,
) -> None:
    """Run the full training loop.

    Phases per episode (one episode = one issue):
    1. Clone repo for this issue
    2. Allocate budgets
    3. Execute all workspaces in parallel
    4. Collect patches via submit_patch
    5. Evaluate and select (η computation + eviction)
    6. Post-episode (mutation / skill review)
    7. Replace evicted workspaces
    8. Clean up repo
    """
    # -- Wire up all components --
    storage = InMemoryStorageBackend()
    hooks = HookSet()
    serial_queue = SerialQueue()
    training_log = TrainingLog(
        storage=storage, hooks=hooks, serial_queue=serial_queue,
    )

    llm_provider = _make_llm_provider(config.model, config.api_key, config.api_base)
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

    eval_provider = _make_llm_provider(
        config.eval_model or config.model,
        config.eval_api_key or config.api_key,
        config.eval_api_base or config.api_base,
    )
    from midas_agent.evaluation.swebench_scorer import SWEBenchScorer
    execution_scorer = SWEBenchScorer(timeout=1800)
    criteria_cache = CriteriaCache(cache_dir="/tmp/midas_criteria")
    llm_judge = LLMJudge(
        llm_provider=eval_provider, criteria_cache=criteria_cache,
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

    # -- Load issues --
    if issues is None:
        issues = load_swe_bench()

    patches_base_dir = "/tmp/patches"

    # -- Create workspaces once --
    scheduler.create_workspaces()

    # -- Episode loop --
    for episode_idx, issue in enumerate(issues):
        logger.info(
            "Episode %d/%d: %s (%s)",
            episode_idx + 1, len(issues), issue.issue_id, issue.repo,
        )

        # 1. Clone repo
        repo_dir = tempfile.mkdtemp(prefix=f"midas_repo_{episode_idx}_")
        try:
            if issue.base_commit and issue.repo:
                clone_repo(issue.repo, issue.base_commit, repo_dir)
                logger.info("  Cloned %s @ %s", issue.repo, issue.base_commit[:8])
            else:
                logger.info("  No repo to clone (dry run)")

            # 2. Set current issue and allocate budgets
            scheduler.set_current_issue(issue)
            scheduler.allocate_budgets()

            # 3. Execute all workspaces (each gets its own repo copy)
            workspaces = scheduler.get_workspaces()
            ws_repo_dirs: list[str] = []
            for ws in workspaces:
                if os.path.isdir(os.path.join(repo_dir, ".git")):
                    ws_repo = os.path.join(repo_dir + "_workspaces", ws.workspace_id)
                    shutil.copytree(repo_dir, ws_repo)
                    ws.work_dir = ws_repo
                    ws_repo_dirs.append(ws_repo)
                ws.execute(issue)

            # 4. Submit patches
            for ws in workspaces:
                ws.submit_patch()

            # 5. Evaluate and select
            patches = collect_patches(workspaces, patches_base_dir)
            evicted, survivors, eval_results = scheduler.evaluate_and_select(patches)

            logger.info(
                "  Scores: %s",
                {ws_id: f"{r.s_w:.3f}" for ws_id, r in eval_results.items()},
            )
            if evicted:
                logger.info("  Evicted: %s", evicted)

            # 6. Post-episode
            new_configs: list[dict] = []
            for ws in workspaces:
                result = ws.post_episode(
                    eval_results={
                        ws_id: {"s_w": r.s_w, "s_exec": r.s_exec}
                        for ws_id, r in eval_results.items()
                    },
                    evicted_ids=evicted,
                )
                if result is not None:
                    new_configs.append(result)

            # 7. Replace evicted workspaces
            scheduler.replace_evicted(new_configs)

        finally:
            # 8. Clean up repo and workspace copies
            shutil.rmtree(repo_dir, ignore_errors=True)
            shutil.rmtree(repo_dir + "_workspaces", ignore_errors=True)

    logger.info("Training complete. %d episodes.", len(issues))
