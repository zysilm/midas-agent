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
from midas_agent.scheduler.storage import InMemoryStorageBackend, LogFilter
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
    """Minimal LLM provider for offline/test usage.

    Returns a task_done tool call so the agent terminates gracefully
    and produces at least one action log entry.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        from midas_agent.llm.types import ToolCall

        # If the request has tools available, respond with task_done
        # so the agent terminates and writes an action log entry.
        if request.tools:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="stub-done", name="task_done", arguments={"result": "stub"})],
                usage=TokenUsage(input_tokens=0, output_tokens=0),
            )
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


def _resolve_swebench_image(issue: Issue) -> str:
    """Resolve the SWE-bench Docker image name for an issue.

    Uses swebench's make_test_spec to get the correct env image key,
    then prepends the DockerHub namespace.
    """
    try:
        from swebench.harness.test_spec.test_spec import make_test_spec
        from datasets import load_dataset

        ds = load_dataset(
            "princeton-nlp/SWE-bench_Verified", split="test",
        )
        for row in ds:
            if row["instance_id"] == issue.issue_id:
                spec = make_test_spec(dict(row), namespace="swebench")
                # Use the instance (eval) image — it has the repo installed
                # with all dependencies. The env image is not on DockerHub.
                # instance_image_key already includes the namespace prefix.
                return spec.instance_image_key
    except Exception as e:
        logger.warning("Could not resolve SWE-bench image: %s", e)

    # Fallback: generic Python image
    return "python:3.11-slim"


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


def collect_patches(workspaces, patches_base_dir: str = "") -> dict[str, str]:
    """Read patches from workspace objects (authoritative source)."""
    return {ws.workspace_id: ws._last_patch for ws in workspaces}


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
        hooks=hooks,
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
            containers: list = []  # ContainerManager instances to clean up

            for ws in workspaces:
                if os.path.isdir(os.path.join(repo_dir, ".git")):
                    ws_repo = os.path.join(repo_dir + "_workspaces", ws.workspace_id)
                    shutil.copytree(repo_dir, ws_repo)
                    ws.work_dir = ws_repo
                    ws_repo_dirs.append(ws_repo)

                # Docker mode: start container, inject all Docker actions
                if config.execution_env == "docker":
                    try:
                        from midas_agent.docker.container_manager import ContainerManager
                        from midas_agent.stdlib.actions.docker_actions import (
                            DockerBashAction,
                            DockerReadFileAction,
                            DockerEditFileAction,
                            DockerWriteFileAction,
                            DockerSearchCodeAction,
                            DockerFindFilesAction,
                        )

                        cm = ContainerManager()
                        image = _resolve_swebench_image(issue)
                        cid = cm.start(
                            image=image,
                            host_workspace=None,  # no mount — all ops inside container
                            install_cmd=None,  # conda testbed env already has repo installed
                        )
                        containers.append(cm)
                        # Inject all Docker actions into the workspace
                        if hasattr(ws, "_action_overrides"):
                            ws._action_overrides = {
                                "bash": DockerBashAction(container_id=cid),
                                "read_file": DockerReadFileAction(container_id=cid),
                                "edit_file": DockerEditFileAction(container_id=cid),
                                "write_file": DockerWriteFileAction(container_id=cid),
                                "search_code": DockerSearchCodeAction(container_id=cid),
                                "find_files": DockerFindFilesAction(container_id=cid),
                            }
                        logger.info("  %s: Docker container %s", ws.workspace_id, cid)
                    except Exception as e:
                        logger.warning(
                            "  %s: Docker setup failed (%s), falling back to local",
                            ws.workspace_id, e,
                        )

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
            # 8. Clean up containers and repo copies
            for cm in containers:
                try:
                    cm.stop()
                except Exception:
                    pass
            shutil.rmtree(repo_dir, ignore_errors=True)
            shutil.rmtree(repo_dir + "_workspaces", ignore_errors=True)

    # -- Close action log files (remove empty files from unexecuted workspaces) --
    workspace_manager.close_all_action_logs(remove_empty=True)

    # -- Export training artifacts --
    _export_artifacts(config, scheduler, training_log)

    logger.info("Training complete. %d episodes.", len(issues))


def _export_artifacts(
    config: MidasConfig,
    scheduler: Scheduler,
    training_log: TrainingLog | None = None,
) -> None:
    """Export training artifacts to disk after training completes."""
    try:
        _do_export(config, scheduler, training_log)
    except Exception as e:
        logger.debug("Export skipped: %s", e)


def _do_export(
    config: MidasConfig,
    scheduler: Scheduler,
    training_log: TrainingLog | None = None,
) -> None:
    import os

    output_dir = "/tmp/midas_output"
    os.makedirs(output_dir, exist_ok=True)

    workspaces = scheduler.get_workspaces()
    if not workspaces:
        return

    if config.runtime_mode == "graph_emergence":
        from midas_agent.inference.schemas import GraphEmergenceArtifact

        # Collect all workspace IDs that were evicted across training.
        evicted_ws_ids = scheduler.get_all_evicted_ever()

        # Pick the first workspace's responsible agent (in a full implementation
        # we'd pick the highest-eta one).
        ws = workspaces[0]
        resp_agent = getattr(ws, "_responsible_agent", None)
        if resp_agent is None:
            return

        fa_manager = getattr(ws, "_free_agent_manager", None)
        free_agents = []
        agent_prices: dict[str, int] = {}
        agent_bankruptcy_rates: dict[str, float] = {}
        if fa_manager:
            for agent_id, agent in fa_manager.free_agents.items():
                free_agents.append(agent)
                agent_prices[agent.agent_id] = fa_manager._pricing_engine.calculate_price(agent)

                # Compute bankruptcy_rate: fraction of workspaces this
                # agent served that were evicted via budget exhaustion.
                br = 0.0
                if training_log is not None:
                    consume_entries = training_log.get_log_entries(
                        LogFilter(entity_id=agent.agent_id, type="consume")
                    )
                    served = {
                        e.workspace_id
                        for e in consume_entries
                        if e.workspace_id
                    }
                    if served:
                        br = len(served & evicted_ws_ids) / len(served)
                agent_bankruptcy_rates[agent.agent_id] = br

        # Gather training metadata
        last_etas: dict[str, float] = {}
        if hasattr(scheduler, '_last_etas'):
            last_etas = scheduler._last_etas

        adaptive_multiplier_value = 1.0
        if hasattr(scheduler, '_budget_allocator') and hasattr(scheduler._budget_allocator, '_adaptive_multiplier'):
            adaptive_multiplier_value = scheduler._budget_allocator._adaptive_multiplier.value

        total_episodes = getattr(scheduler, '_episode_count', 0)

        artifact = GraphEmergenceArtifact(
            responsible_agent=resp_agent,
            free_agents=free_agents,
            agent_prices=agent_prices,
            agent_bankruptcy_rates=agent_bankruptcy_rates,
            last_etas=last_etas,
            adaptive_multiplier_value=adaptive_multiplier_value,
            total_episodes=total_episodes,
            budget_hint=config.initial_budget,
        )

        output_path = os.path.join(output_dir, "graph_emergence_artifact.json")
        with open(output_path, "w") as f:
            f.write(artifact.model_dump_json(indent=2))
        logger.info("Exported Graph Emergence artifact to %s", output_path)

    else:
        # Config Evolution: export best config from snapshot store.
        from midas_agent.workspace.config_evolution.snapshot_store import SnapshotFilter

        ws = workspaces[0]
        snapshot_store = getattr(ws, "_snapshot_store", None)
        if snapshot_store is None:
            return

        snapshots = snapshot_store.query(SnapshotFilter(top_k=1))
        if not snapshots:
            logger.info("No snapshots to export.")
            return

        output_path = os.path.join(output_dir, "best_config.yaml")
        with open(output_path, "w") as f:
            f.write(snapshots[0].config_yaml)
        logger.info("Exported best config (η=%.4f) to %s", snapshots[0].eta, output_path)
