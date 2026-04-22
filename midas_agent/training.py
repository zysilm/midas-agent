"""Training entry point — episode loop orchestration."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

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
    """Create an LLM provider from config.  Raises if model is empty."""
    if not model:
        raise ValueError(
            "No LLM model configured.  Set MIDAS_MODEL environment variable "
            "or add 'model' to .midas/config.yaml."
        )
    from midas_agent.llm.litellm_provider import LiteLLMProvider
    return LiteLLMProvider(
        model=model,
        api_key=api_key or None,
        api_base=api_base or None,
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


# ------------------------------------------------------------------
# Checkpoint helpers
# ------------------------------------------------------------------

def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically (temp file + rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _save_checkpoint(
    train_dir: str,
    episode_idx: int,
    processed_issue_ids: list[str],
    workspaces: list,
    scheduler,
    adaptive_ctrl=None,
) -> None:
    """Save checkpoint after a completed episode."""
    # Map workspace → latest config YAML filename
    ws_configs = {}
    for ws in workspaces:
        ep_count = getattr(ws, "_episode_count", 0)
        ws_configs[ws.workspace_id] = f"{ws.workspace_id}_ep{ep_count}.yaml"

    # Get adaptive multiplier value
    mult_value = 1.0
    if hasattr(scheduler, "_budget_allocator"):
        ba = scheduler._budget_allocator
        if hasattr(ba, "_adaptive_multiplier"):
            mult_value = ba._adaptive_multiplier.value

    # Get GEPA counter from first workspace
    gepa_counter = 0
    for ws in workspaces:
        opt = getattr(ws, "_prompt_optimizer", None)
        if opt and hasattr(opt, "_episodes_since_last_optimization"):
            gepa_counter = opt._episodes_since_last_optimization
            break

    checkpoint = {
        "episode_idx": episode_idx,
        "processed_issue_ids": processed_issue_ids,
        "workspace_configs": ws_configs,
        "adaptive_multiplier_value": mult_value,
        "gepa_episodes_since_optimization": gepa_counter,
        "timestamp": datetime.now().isoformat(),
    }

    # Save adaptive workspace controller state
    if adaptive_ctrl:
        checkpoint["adaptive_controller"] = adaptive_ctrl.to_dict()

    _atomic_write_json(os.path.join(train_dir, "checkpoint.json"), checkpoint)
    logger.info("Checkpoint saved: episode %d", episode_idx)


def _load_checkpoint(train_dir: str) -> dict | None:
    """Load checkpoint if it exists. Returns None if no checkpoint."""
    path = os.path.join(train_dir, "checkpoint.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _save_swebench_artifacts(
    train_dir: str,
    issue: Issue,
    workspaces: list,
    eval_results: dict,
) -> None:
    """Save SWE-bench leaderboard submission artifacts per episode.

    - Appends best workspace's patch to all_preds.jsonl
    - Saves reasoning trace to trajs/{instance_id}.md
    """
    # Find best workspace by s_w score
    best_ws = None
    best_score = -1.0
    for ws in workspaces:
        result = eval_results.get(ws.workspace_id)
        if result and result.s_w > best_score:
            best_score = result.s_w
            best_ws = ws

    if best_ws is None:
        return

    patch = getattr(best_ws, "_last_patch", "") or ""

    # Append to all_preds.jsonl
    preds_path = os.path.join(train_dir, "all_preds.jsonl")
    pred = {
        "instance_id": issue.issue_id,
        "model_name_or_path": "midas-agent",
        "model_patch": patch,
    }
    with open(preds_path, "a") as f:
        f.write(json.dumps(pred) + "\n")

    # Save reasoning trace
    trajs_dir = os.path.join(train_dir, "trajs")
    os.makedirs(trajs_dir, exist_ok=True)
    traj_path = os.path.join(trajs_dir, f"{issue.issue_id}.md")

    last_result = getattr(best_ws, "_last_result", None)
    trace_lines = [f"# {issue.issue_id}\n"]
    trace_lines.append(f"**Score**: {best_score:.3f}\n")
    trace_lines.append(f"**Workspace**: {best_ws.workspace_id}\n\n")

    if last_result and last_result.action_history:
        trace_lines.append("## Trace\n\n```\n")
        from midas_agent.workspace.config_evolution.config_creator import format_trace
        trace_lines.append(format_trace(last_result.action_history))
        trace_lines.append("\n```\n")

    if patch:
        trace_lines.append("\n## Patch\n\n```diff\n")
        trace_lines.append(patch[:5000])  # cap at 5K chars
        if len(patch) > 5000:
            trace_lines.append(f"\n... ({len(patch) - 5000} more chars)")
        trace_lines.append("\n```\n")

    with open(traj_path, "w") as f:
        f.write("".join(trace_lines))


def _rebuild_workspace_config(train_dir: str, config_filename: str):
    """Rebuild a WorkflowConfig from a saved YAML file."""
    from midas_agent.workspace.config_evolution.config_creator import _parse_config_yaml

    path = os.path.join(train_dir, "log", "configs", config_filename)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return _parse_config_yaml(f.read())


def _find_latest_train_dir() -> str | None:
    """Find the most recent .midas/train/<ts>/ with a checkpoint.json."""
    train_root = os.path.join(".midas", "train")
    if not os.path.isdir(train_root):
        return None
    candidates = []
    for name in sorted(os.listdir(train_root), reverse=True):
        path = os.path.join(train_root, name)
        if os.path.isfile(os.path.join(path, "checkpoint.json")):
            candidates.append(path)
    return candidates[0] if candidates else None


def run_training(
    config: MidasConfig,
    issues: list[Issue] | None = None,
    fresh: bool = False,
    resume_dir: str | None = None,
    config_path: str | None = None,
    train_dir_name: str | None = None,
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
    # -- Resolve training directory --
    if resume_dir and resume_dir != "auto":
        train_dir = resume_dir
    elif resume_dir == "auto" and not fresh:
        train_dir = _find_latest_train_dir()
        if train_dir:
            logger.info("Auto-detected training directory: %s", train_dir)
        else:
            train_dir = None  # will create new below
    else:
        train_dir = None

    if train_dir is None:
        name = train_dir_name or datetime.now().strftime("%Y%m%d_%H%M%S")
        train_dir = os.path.join(".midas", "train", name)

    os.makedirs(os.path.join(train_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(train_dir, "log", "configs"), exist_ok=True)
    os.makedirs(os.path.join(train_dir, "log", "action_logs"), exist_ok=True)

    # Save training config into train_dir for resume
    saved_config = os.path.join(train_dir, "train_config.yaml")
    if config_path and not os.path.isfile(saved_config):
        import shutil
        shutil.copy2(config_path, saved_config)

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
        train_dir=train_dir,
    )

    eval_provider = _make_llm_provider(
        config.eval_model or config.model,
        config.eval_api_key or config.api_key,
        config.eval_api_base or config.api_base,
    )
    from midas_agent.evaluation.swebench_scorer import SWEBenchScorer
    execution_scorer = SWEBenchScorer(timeout=1800)
    criteria_cache = CriteriaCache(
        cache_dir=os.path.join(train_dir, "log", "criteria_cache"),
    )
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

    patches_base_dir = os.path.join(train_dir, "log", "patches")

    # -- Create workspaces and adaptive controller --
    from midas_agent.scheduler.adaptive_workspace import AdaptiveWorkspaceController

    adaptive_ctrl = AdaptiveWorkspaceController() if config.adaptive_workspaces else None
    scheduler.create_workspaces()
    if adaptive_ctrl:
        workspaces = scheduler.get_workspaces()
        if workspaces:
            adaptive_ctrl.init_champion(workspaces[0].workspace_id)

    # -- Resume from checkpoint if available --
    processed_issue_ids: list[str] = []
    checkpoint = _load_checkpoint(train_dir) if not fresh else None
    if checkpoint:
        processed_ids_set = set(checkpoint["processed_issue_ids"])
        processed_issue_ids = list(checkpoint["processed_issue_ids"])

        # Restore workspace configs from saved YAML
        workspaces = scheduler.get_workspaces()
        for ws in workspaces:
            ws_configs = checkpoint.get("workspace_configs", {})
            config_file = ws_configs.get(ws.workspace_id)
            if config_file:
                restored_config = _rebuild_workspace_config(train_dir, config_file)
                if restored_config:
                    # Infer episode count from filename (ws-0_ep5.yaml → 5)
                    ep_count = int(config_file.split("_ep")[1].split(".")[0]) if "_ep" in config_file else 0
                    ws.restore_state(restored_config, ep_count)

        # Reload GEPA dataset
        for ws in workspaces:
            opt = getattr(ws, "_prompt_optimizer", None)
            if opt and hasattr(opt, "load_dataset_from_dir"):
                data_dir = os.path.join(train_dir, "data")
                opt.load_dataset_from_dir(data_dir)
                opt._episodes_since_last_optimization = checkpoint.get(
                    "gepa_episodes_since_optimization", 0,
                )

        # Restore adaptive multiplier
        mult_val = checkpoint.get("adaptive_multiplier_value", 1.0)
        if hasattr(scheduler, "_budget_allocator"):
            ba = scheduler._budget_allocator
            if hasattr(ba, "_adaptive_multiplier"):
                ba._adaptive_multiplier._value = mult_val

        # Filter out already-processed issues
        issues = [i for i in issues if i.issue_id not in processed_ids_set]
        # Restore adaptive controller state
        if adaptive_ctrl and "adaptive_controller" in checkpoint:
            from midas_agent.scheduler.adaptive_workspace import AdaptiveWorkspaceController
            adaptive_ctrl = AdaptiveWorkspaceController.from_dict(
                checkpoint["adaptive_controller"]
            )

        logger.info(
            "Resumed from checkpoint: %d episodes done, %d remaining",
            len(processed_issue_ids), len(issues),
        )

    # -- Episode loop --
    for episode_idx, issue in enumerate(issues):
        global_idx = len(processed_issue_ids) + episode_idx
        logger.info(
            "Episode %d/%d: %s (%s)",
            global_idx + 1, global_idx + len(issues), issue.issue_id, issue.repo,
        )

        # 1. Clone repo
        repo_dir = tempfile.mkdtemp(prefix=f"midas_repo_{global_idx}_")
        try:
            if issue.base_commit and issue.repo:
                clone_repo(issue.repo, issue.base_commit, repo_dir)
                logger.info("  Cloned %s @ %s", issue.repo, issue.base_commit[:8])
            else:
                logger.info("  No repo to clone (dry run)")

            # 2. Set current issue and allocate budgets
            scheduler.set_current_issue(issue)
            scheduler.allocate_budgets()

            # 3. Setup and execute all workspaces in parallel
            from concurrent.futures import ThreadPoolExecutor, as_completed

            workspaces = scheduler.get_workspaces()
            ws_repo_dirs: list[str] = []
            containers: list = []  # ContainerManager instances to clean up

            def _setup_and_execute(ws):
                """Setup Docker + execute for one workspace. Runs in a thread."""
                if os.path.isdir(os.path.join(repo_dir, ".git")):
                    ws_repo = os.path.join(repo_dir + "_workspaces", ws.workspace_id)
                    shutil.copytree(repo_dir, ws_repo)
                    ws.work_dir = ws_repo
                    ws_repo_dirs.append(ws_repo)

                # Docker mode: start container, set IO backend
                if config.execution_env == "docker":
                    try:
                        from midas_agent.docker.container_manager import ContainerManager
                        from midas_agent.runtime.io_backend import DockerIO

                        cm = ContainerManager()
                        image = _resolve_swebench_image(issue)
                        cid = cm.start(
                            image=image,
                            host_workspace=None,
                            install_cmd=None,
                        )
                        containers.append(cm)
                        docker_io = DockerIO(container_id=cid, workdir="/testbed")
                        if hasattr(ws, "_io"):
                            ws._io = docker_io
                        logger.info("  %s: Docker container %s", ws.workspace_id, cid)
                    except Exception as e:
                        logger.warning(
                            "  %s: Docker setup failed (%s), falling back to local",
                            ws.workspace_id, e,
                        )

                ws.execute(issue)
                return ws.workspace_id

            # Run all workspaces in parallel
            with ThreadPoolExecutor(max_workers=len(workspaces)) as executor:
                futures = {executor.submit(_setup_and_execute, ws): ws for ws in workspaces}
                for future in as_completed(futures):
                    ws = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        logger.warning("  %s: execution failed: %s", ws.workspace_id, e)

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

            # 6. Post-episode (config creation / GEPA optimization)
            eval_results_dict = {
                ws_id: {"s_w": r.s_w, "s_exec": r.s_exec}
                for ws_id, r in eval_results.items()
            }
            for ws in workspaces:
                ws.post_episode(eval_results_dict, evicted_ids=evicted)

            # 7. Adaptive workspace management or fixed eviction
            if adaptive_ctrl:
                # Record η for each workspace
                for ws in workspaces:
                    r = eval_results.get(ws.workspace_id)
                    if r:
                        cost = max(1, ws.budget_received)
                        eta = r.s_w / cost
                        adaptive_ctrl.record_episode(ws.workspace_id, eta)

                # Check if any workspace's GEPA changed its config
                gepa_changes = {
                    ws.workspace_id: getattr(ws, "_last_gepa_changed", False)
                    for ws in workspaces
                }
                any_gepa_ran = any(gepa_changes.values()) or any(
                    getattr(ws, "_prompt_optimizer", None)
                    and not getattr(ws, "_prompt_optimizer").should_optimize()
                    and getattr(ws, "_prompt_optimizer")._episodes_since_last_optimization == 0
                    for ws in workspaces
                )

                if any_gepa_ran:
                    champ_id = adaptive_ctrl.champion_stats.workspace_id if adaptive_ctrl.champion_stats else None
                    chall_id = adaptive_ctrl.challenger_stats.workspace_id if adaptive_ctrl.challenger_stats else None

                    champ_changed = gepa_changes.get(champ_id, False)
                    chall_changed = gepa_changes.get(chall_id, False) if chall_id else None

                    result = adaptive_ctrl.on_gepa_result(champ_changed, chall_changed)

                    if result["action"] == "start_h2h":
                        # Create a challenger workspace with the champion's NEW config
                        new_id = f"ws-challenger-{global_idx}"
                        best_config = scheduler._get_best_config()
                        scheduler._workspace_manager.replace(
                            champ_id, champ_id, best_config,
                        ) if False else None  # champion keeps its config
                        ws_new = scheduler._workspace_manager.create(new_id, best_config)
                        adaptive_ctrl.start_head_to_head(new_id)

                    elif result["action"] == "select_winner":
                        loser_id = result.get("loser_id")
                        if loser_id:
                            scheduler._workspace_manager.destroy(loser_id)
                            logger.info("  Adaptive: removed loser %s", loser_id)
            else:
                # Fixed eviction mode
                scheduler.replace_evicted()

            # 8. Save SWE-bench submission artifacts
            _save_swebench_artifacts(
                train_dir, issue, workspaces, eval_results,
            )

            # 10. Save checkpoint
            processed_issue_ids.append(issue.issue_id)
            _save_checkpoint(
                train_dir, global_idx, processed_issue_ids,
                workspaces, scheduler, adaptive_ctrl,
            )

        finally:
            # 11. Clean up containers and repo copies
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
    _export_artifacts(config, scheduler, training_log, train_dir)

    logger.info("Training complete. %d episodes. Artifacts in %s", len(issues), train_dir)


def _export_artifacts(
    config: MidasConfig,
    scheduler: Scheduler,
    training_log: TrainingLog | None = None,
    train_dir: str = "/tmp/midas_output",
) -> None:
    """Export training artifacts to disk after training completes."""
    try:
        _do_export(config, scheduler, training_log, train_dir)
    except Exception as e:
        logger.debug("Export skipped: %s", e)


def _do_export(
    config: MidasConfig,
    scheduler: Scheduler,
    training_log: TrainingLog | None = None,
    train_dir: str = "/tmp/midas_output",
) -> None:
    import os

    output_dir = os.path.join(train_dir, "log")
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
