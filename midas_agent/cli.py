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
    train_parser.add_argument("--config", required=True, help="Path to training config YAML")
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

    # -- infer subcommand --
    infer_parser = subparsers.add_parser("infer", help="Run inference")
    infer_parser.add_argument("--artifact", default=None, help="Path to artifact file")
    infer_parser.add_argument("--model", default=None, help="LLM model name")
    infer_parser.add_argument("--budget", default=None, type=int, help="Token budget")
    infer_parser.add_argument(
        "--env",
        default="local",
        help='Execution environment: "local" or "docker" (default: local)',
    )

    return parser.parse_args(argv)


def build_action_set(cwd: str, env: str = "local") -> list[Action]:
    """Build a list of Action instances for inference mode."""
    if env == "docker":
        from midas_agent.stdlib.actions.docker_actions import (
            DockerBashAction,
            DockerEditFileAction,
            DockerFindFilesAction,
            DockerReadFileAction,
            DockerSearchCodeAction,
            DockerWriteFileAction,
        )
        from midas_agent.stdlib.actions.task_done import TaskDoneAction
        from midas_agent.stdlib.actions.update_plan import UpdatePlanAction

        # Docker actions require a container_id; use a placeholder for now.
        # The real container_id is set at runtime before any action executes.
        container_id = ""
        return [
            DockerBashAction(container_id=container_id, cwd=cwd),
            DockerReadFileAction(container_id=container_id, cwd=cwd),
            DockerEditFileAction(container_id=container_id, cwd=cwd),
            DockerWriteFileAction(container_id=container_id, cwd=cwd),
            DockerSearchCodeAction(container_id=container_id, cwd=cwd),
            DockerFindFilesAction(container_id=container_id, cwd=cwd),
            UpdatePlanAction(),
            TaskDoneAction(),
        ]

    # Default: local environment
    from midas_agent.stdlib.actions.bash import BashAction
    from midas_agent.stdlib.actions.file_ops import (
        EditFileAction,
        ReadFileAction,
        WriteFileAction,
    )
    from midas_agent.stdlib.actions.search import FindFilesAction, SearchCodeAction
    from midas_agent.stdlib.actions.task_done import TaskDoneAction
    from midas_agent.stdlib.actions.update_plan import UpdatePlanAction

    return [
        BashAction(cwd=cwd),
        ReadFileAction(cwd=cwd),
        EditFileAction(cwd=cwd),
        WriteFileAction(cwd=cwd),
        SearchCodeAction(cwd=cwd),
        FindFilesAction(cwd=cwd),
        UpdatePlanAction(),
        TaskDoneAction(),
    ]


def _cmd_train(args: argparse.Namespace) -> None:
    """Execute the train subcommand."""
    from midas_agent.resolver import ConfigurationError, resolve_llm_config

    try:
        llm_config = resolve_llm_config(cli_model=None, cli_api_key=None)
    except ConfigurationError as e:
        print(str(e))
        sys.exit(1)

    # Load training config from YAML
    import yaml

    with open(args.config) as f:
        raw = yaml.safe_load(f) or {}

    from midas_agent.config import MidasConfig

    # LLM credentials come from env vars (via resolver), not the YAML.
    # Strip any model/api_key/api_base from the YAML so resolver wins.
    config_kwargs = {k: v for k, v in raw.items() if k not in ("model", "api_key", "api_base")}
    config = MidasConfig(
        model=llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base or "",
        **config_kwargs,
    )

    from midas_agent.training import load_swe_bench, run_training

    issues = load_swe_bench()
    if args.issues is not None:
        issues = issues[: args.issues]
    elif args.issue_index is not None:
        if args.issue_index >= len(issues):
            print(f"Error: --issue-index {args.issue_index} is out of range (dataset has {len(issues)} issues)")
            sys.exit(1)
        issues = [issues[args.issue_index]]

    print(f"Training: {len(issues)} issues, budget={config.initial_budget}")
    run_training(config, issues=issues)


def _cmd_infer(args: argparse.Namespace) -> None:
    """Execute the infer subcommand."""
    from midas_agent.resolver import ConfigurationError, resolve_artifact_path, resolve_llm_config

    try:
        llm_config = resolve_llm_config(cli_model=args.model, cli_api_key=None)
    except ConfigurationError as e:
        print(str(e))
        sys.exit(1)

    artifact_path = resolve_artifact_path(explicit=args.artifact)

    from midas_agent.inference.schemas import GraphEmergenceArtifact

    with open(artifact_path) as f:
        artifact = GraphEmergenceArtifact.model_validate_json(f.read())

    from midas_agent.llm.litellm_provider import LiteLLMProvider

    llm_provider = LiteLLMProvider(
        model=llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base,
    )

    actions = build_action_set(cwd=os.getcwd(), env=args.env)

    budget = args.budget or artifact.budget_hint

    from midas_agent.inference.production_meter import ProductionResourceMeter

    meter = ProductionResourceMeter(llm_provider, budget)
    call_llm = lambda req: meter.process(req)

    from midas_agent.tui import TUI

    tui = TUI(
        call_llm=call_llm,
        actions=actions,
        system_prompt=artifact.responsible_agent.soul.system_prompt,
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
