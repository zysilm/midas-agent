"""CLI argument parsing and action-set building for Midas Agent."""
from __future__ import annotations

import argparse
import os
import sys

from midas_agent.stdlib.action import Action


def _nonneg_int(value: str) -> int:
    """Argparse type: non-negative integer."""
    i = int(value)
    if i < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {i}")
    return i


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments with train/infer subcommands."""
    parser = argparse.ArgumentParser(prog="midas", description="Midas Agent CLI")
    subparsers = parser.add_subparsers(dest="command", required=False)

    # -- train subcommand --
    train_parser = subparsers.add_parser("train", help="Run training pipeline")
    train_parser.add_argument("--config", default=None, help="Path to training config YAML (optional with --resume)")
    train_parser.add_argument(
        "--output",
        default=".midas/agents/",
        help="Output directory for artifacts (default: .midas/agents/)",
    )
    issue_group = train_parser.add_mutually_exclusive_group()
    issue_group.add_argument(
        "--issues",
        type=int,
        default=None,
        help="Number of issues to train on (default: all)",
    )
    issue_group.add_argument(
        "--issue-index",
        type=_nonneg_int,
        default=None,
        help="Train on a single issue by its 0-based index in the dataset",
    )
    train_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing checkpoint, start fresh training",
    )
    train_parser.add_argument(
        "--resume",
        default=None,
        nargs="?",
        const="auto",
        help="Resume from a training directory. No value = auto-detect latest.",
    )
    train_parser.add_argument(
        "--train-dir",
        default=None,
        help="Custom training directory name (default: auto-generated timestamp)",
    )

    # -- infer subcommand --
    infer_parser = subparsers.add_parser("infer", help="Run inference with a DAG config")
    infer_parser.add_argument("--dag", required=True, help="Path to DAG config YAML")
    infer_parser.add_argument("--model", default=None, help="LLM model name")
    infer_parser.add_argument("--budget", default=None, type=int, help="Token budget (default: 1500000)")
    infer_parser.add_argument(
        "--issues",
        type=int,
        default=None,
        help="Run on N SWE-bench issues (eval mode). Omit for interactive TUI.",
    )
    infer_parser.add_argument(
        "--issue-index",
        type=_nonneg_int,
        default=None,
        help="Run on a single issue by its 0-based index",
    )
    infer_parser.add_argument(
        "--lessons",
        default=None,
        help="Path to lessons.json from training (enables lesson retrieval)",
    )
    infer_parser.add_argument(
        "--env",
        default="docker",
        help='Execution environment: "local" or "docker" (default: docker)',
    )

    return parser.parse_args(argv)


def build_action_set(cwd: str, env: str = "local") -> list[Action]:
    """Build a list of Action instances for inference mode.

    Same action classes for both local and docker modes — only the IO
    backend differs. For docker mode, the IO backend is set later at
    runtime when a container is available.
    """
    from midas_agent.stdlib.actions.bash import BashAction
    from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction

    return [
        BashAction(cwd=cwd),
        StrReplaceEditorAction(cwd=cwd),
    ]


def _cmd_train(args: argparse.Namespace) -> None:
    """Execute the train subcommand."""
    import shutil

    import yaml

    from midas_agent.config import MidasConfig
    from midas_agent.resolver import ConfigurationError, resolve_llm_config
    from midas_agent.training import load_swe_bench, run_training

    resume = getattr(args, "resume", None)
    fresh = getattr(args, "fresh", False)

    if not args.config and not resume:
        print("Error: --config is required (or use --resume to continue a previous run)")
        sys.exit(1)

    # On resume, load training config from the resume directory
    if resume and not fresh:
        from midas_agent.training import _find_latest_train_dir

        resume_dir = resume if resume != "auto" else _find_latest_train_dir()
        if resume_dir:
            saved_config = os.path.join(resume_dir, "train_config.yaml")
            if os.path.isfile(saved_config):
                print(f"Resuming from {resume_dir} (using saved config)")
                config_path = saved_config
            elif args.config:
                print(f"Resuming from {resume_dir} (no saved config, using --config)")
                config_path = args.config
            else:
                print(f"Error: {resume_dir} has no saved config and --config not provided")
                sys.exit(1)
        else:
            print("No checkpoint found, starting fresh")
            resume = None
            config_path = args.config
    else:
        config_path = args.config
        if not config_path:
            print("Error: --config is required for fresh training")
            sys.exit(1)

    try:
        llm_config = resolve_llm_config(cli_model=None, cli_api_key=None)
    except ConfigurationError as e:
        print(str(e))
        sys.exit(1)

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    # LLM credentials come from env vars (via resolver), not the YAML.
    config_kwargs = {k: v for k, v in raw.items() if k not in ("model", "api_key", "api_base")}
    config = MidasConfig(
        model=llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base or "",
        **config_kwargs,
    )

    issues = load_swe_bench()
    if args.issues is not None:
        issues = issues[: args.issues]
    elif args.issue_index is not None:
        if args.issue_index >= len(issues):
            print(f"Error: --issue-index {args.issue_index} is out of range (dataset has {len(issues)} issues)")
            sys.exit(1)
        issues = [issues[args.issue_index]]

    train_dir_name = getattr(args, "train_dir", None)
    print(f"Training: {len(issues)} issues, budget={config.initial_budget}")
    run_training(
        config, issues=issues, fresh=fresh, resume_dir=resume,
        config_path=config_path, train_dir_name=train_dir_name,
    )


def _cmd_infer(args: argparse.Namespace) -> None:
    """Execute the infer subcommand.

    Two modes:
      - Interactive TUI (default): load DAG config, launch REPL
      - Eval (--issues N): run DAG on SWE-bench issues, report scores
    """
    import logging
    import time
    import yaml

    from midas_agent.resolver import ConfigurationError, resolve_llm_config
    from midas_agent.workspace.config_evolution.config_creator import _parse_config_yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger = logging.getLogger("midas-infer")

    try:
        llm_config = resolve_llm_config(cli_model=args.model, cli_api_key=None)
    except ConfigurationError as e:
        print(str(e))
        sys.exit(1)

    # Load DAG config
    with open(args.dag) as f:
        dag_config = _parse_config_yaml(f.read())
    if dag_config is None:
        print(f"Error: failed to parse DAG config from {args.dag}")
        sys.exit(1)

    logger.info("DAG: %s (%d steps)", dag_config.meta.name, len(dag_config.steps))

    from midas_agent.llm.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(
        model=llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base,
    )

    budget = args.budget or 1_500_000

    if args.issues is not None or args.issue_index is not None:
        # -- Eval mode: run on SWE-bench issues --
        _infer_eval(args, dag_config, provider, budget, logger)
    else:
        # -- Interactive TUI mode --
        _infer_tui(dag_config, provider, budget)


def _infer_eval(args, dag_config, provider, budget, logger):
    """Run DAG config on SWE-bench issues with scoring (frozen config)."""
    import time
    from midas_agent.training import load_swe_bench, _resolve_swebench_image
    from midas_agent.stdlib.action import ActionRegistry
    from midas_agent.stdlib.actions.bash import BashAction
    from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
    from midas_agent.workspace.config_evolution.executor import DAGExecutor
    from midas_agent.workspace.config_evolution.config_creator import ConfigMerger
    from midas_agent.docker.container_manager import ContainerManager
    from midas_agent.runtime.io_backend import DockerIO
    from midas_agent.evaluation.swebench_scorer import SWEBenchScorer

    # Load lesson store if provided
    lesson_store = None
    if getattr(args, "lessons", None):
        from midas_agent.workspace.config_evolution.lesson_store import LessonStore
        lesson_store = LessonStore(store_path=args.lessons)
        logger.info("Loaded %d lessons from %s", len(lesson_store), args.lessons)

    issues = load_swe_bench()
    if args.issue_index is not None:
        issues = [issues[args.issue_index]]
    elif args.issues is not None:
        issues = issues[:args.issues]

    logger.info("Eval: %d issues, budget=%d", len(issues), budget)

    def call_llm(req, retries=3):
        for attempt in range(retries):
            try:
                return provider.complete(req)
            except Exception as e:
                if attempt < retries - 1:
                    import time as _t
                    _t.sleep(2 ** attempt)
                else:
                    raise

    system_llm = lambda req: call_llm(req)
    scorer = SWEBenchScorer(timeout=1800)
    results = []

    for i, issue in enumerate(issues):
        logger.info("Issue %d/%d: %s", i + 1, len(issues), issue.issue_id)

        try:
            import subprocess
            subprocess.run(
                ["docker", "rm", "-f"] +
                subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True).stdout.split(),
                capture_output=True,
            )
        except Exception:
            pass

        cm = ContainerManager()
        image = _resolve_swebench_image(issue)
        cid = cm.start(image=image, host_workspace=None, install_cmd=None)
        io = DockerIO(container_id=cid, workdir="/testbed")

        try:
            actions = [BashAction(), StrReplaceEditorAction()]
            for a in actions:
                if hasattr(a, "_io"):
                    a._io = io

            registry = ActionRegistry(actions)
            executor = DAGExecutor(
                action_registry=registry,
                max_tool_output_chars=100000,
                max_context_tokens=32000,
                system_llm=system_llm,
            )

            # Retrieve lessons and merge issue into step prompts
            retrieved_lessons = []
            if lesson_store is not None and len(lesson_store) > 0:
                retrieved_lessons = lesson_store.retrieve(issue.description)
                if retrieved_lessons:
                    logger.info("  Retrieved %d lessons", len(retrieved_lessons))

            merger = ConfigMerger(system_llm=system_llm)
            merged = merger.merge(dag_config, issue, lessons=retrieved_lessons or None)

            t0 = time.time()
            result = executor.execute(merged, issue, call_llm,
                                      balance_provider=lambda: budget)
            elapsed = time.time() - t0

            # Get patch
            try:
                io.run_bash("git add -A")
                patch = io.run_bash("git diff --cached")
                io.run_bash("git reset")
            except Exception:
                patch = ""

            score = scorer.score(patch, issue)
            results.append({"issue": issue.issue_id, "score": score,
                           "iters": len(result.action_history), "patch": len(patch),
                           "time": elapsed})
            logger.info("  Result: score=%.3f, iters=%d, patch=%d chars, time=%.0fs",
                        score, len(result.action_history), len(patch), elapsed)

        except Exception as e:
            logger.error("  FAILED: %s", e)
            results.append({"issue": issue.issue_id, "score": 0.0,
                           "iters": 0, "patch": 0, "time": 0})
        finally:
            cm.stop()

    # Summary
    passed = sum(1 for r in results if r["score"] >= 1.0)
    logger.info("SUMMARY: Pass rate: %d/%d (%d%%)", passed, len(results), 100 * passed // len(results))
    for r in results:
        logger.info("  %s: score=%.3f, iters=%d, patch=%d chars, %.0fs",
                    r["issue"], r["score"], r["iters"], r["patch"], r["time"])


def _infer_tui(dag_config, provider, budget):
    """Interactive TUI with a DAG config."""
    from midas_agent.prompts import SYSTEM_PROMPT

    actions = build_action_set(cwd=os.getcwd(), env="local")

    def call_llm(req):
        return provider.complete(req)

    from midas_agent.tui import TUI

    tui = TUI(
        call_llm=call_llm,
        actions=actions,
        system_prompt=SYSTEM_PROMPT,
    )
    tui.run()


def main(argv: list[str] | None = None) -> None:
    """Entry point for the CLI."""
    args = parse_args(argv or sys.argv[1:])

    if args.command == "train":
        _cmd_train(args)
    else:
        # Default: infer (no subcommand or explicit "infer")
        _cmd_infer(args)
