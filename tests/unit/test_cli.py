"""Unit tests for CLI argument parsing and action building.

Tests define the target behavior of:
- parse_args: argparse-based CLI with train/infer subcommands
- build_action_set: assemble the right actions for inference

Tests are expected to FAIL until the CLI module is implemented.
"""
from __future__ import annotations

import pytest

from midas_agent.cli import build_action_set, parse_args


# ===================================================================
# CLI argument parsing — train subcommand
# ===================================================================


@pytest.mark.unit
class TestTrainArgs:
    """The 'train' subcommand requires --config and has optional --output."""

    def test_train_requires_config(self):
        """train without --config raises SystemExit."""
        with pytest.raises(SystemExit):
            parse_args(["train"])

    def test_train_parses_config(self):
        args = parse_args(["train", "--config", "train.yaml"])
        assert args.command == "train"
        assert args.config == "train.yaml"

    def test_train_default_output(self):
        """Default output is .midas/agents/ relative to cwd."""
        args = parse_args(["train", "--config", "train.yaml"])
        assert ".midas" in args.output

    def test_train_custom_output(self):
        args = parse_args(["train", "--config", "train.yaml", "--output", "/custom/dir"])
        assert args.output == "/custom/dir"


# ===================================================================
# CLI argument parsing — infer subcommand
# ===================================================================


@pytest.mark.unit
class TestInferArgs:
    """The 'infer' subcommand has all optional flags with sensible defaults."""

    def test_infer_no_required_args(self):
        """infer runs with zero flags (uses defaults for everything)."""
        args = parse_args(["infer"])
        assert args.command == "infer"

    def test_infer_artifact_flag(self):
        args = parse_args(["infer", "--artifact", "my_agent.json"])
        assert args.artifact == "my_agent.json"

    def test_infer_model_flag(self):
        args = parse_args(["infer", "--model", "gpt-4o"])
        assert args.model == "gpt-4o"

    def test_infer_budget_flag(self):
        args = parse_args(["infer", "--budget", "50000"])
        assert args.budget == 50000

    def test_infer_env_flag(self):
        args = parse_args(["infer", "--env", "docker"])
        assert args.env == "docker"

    def test_infer_default_env_is_local(self):
        args = parse_args(["infer"])
        assert args.env == "local"

    def test_infer_default_artifact_is_none(self):
        """When no --artifact, args.artifact is None (resolver will handle)."""
        args = parse_args(["infer"])
        assert args.artifact is None

    def test_infer_default_model_is_none(self):
        """When no --model, args.model is None (resolver will handle)."""
        args = parse_args(["infer"])
        assert args.model is None

    def test_infer_default_budget_is_none(self):
        """When no --budget, args.budget is None (uses artifact's budget_hint)."""
        args = parse_args(["infer"])
        assert args.budget is None


# ===================================================================
# CLI general behavior
# ===================================================================


@pytest.mark.unit
class TestGeneralCLI:
    """General CLI behavior: no subcommand, unknown subcommand, etc."""

    def test_no_subcommand_defaults_to_infer(self):
        """Running with no subcommand defaults to infer mode."""
        args = parse_args([])
        assert args.command is None  # main() treats None as infer

    def test_unknown_subcommand_exits(self):
        """Unknown subcommand raises SystemExit."""
        with pytest.raises(SystemExit):
            parse_args(["unknown"])


# ===================================================================
# Action set building
# ===================================================================


@pytest.mark.unit
class TestBuildActionSet:
    """build_action_set assembles the right actions for inference mode."""

    def test_local_mode_has_core_actions(self, tmp_path):
        """Local mode includes bash, read_file, edit_file, write_file, search_code, find_files."""
        actions = build_action_set(cwd=str(tmp_path), env="local")
        names = {a.name for a in actions}
        assert "bash" in names
        assert "read_file" in names
        assert "edit_file" in names
        assert "write_file" in names
        assert "search_code" in names
        assert "find_files" in names

    def test_local_mode_has_task_done(self, tmp_path):
        """Local mode always includes task_done."""
        actions = build_action_set(cwd=str(tmp_path), env="local")
        names = {a.name for a in actions}
        assert "task_done" in names

    def test_docker_mode_has_same_actions(self, tmp_path):
        """Docker mode uses the same action classes with DockerIO backend."""
        actions = build_action_set(cwd=str(tmp_path), env="docker")
        names = {a.name for a in actions}
        # Same action names as local — unified via IO backend
        assert "bash" in names
        assert "read_file" in names
        assert "edit_file" in names

    def test_actions_have_correct_cwd(self, tmp_path):
        """All file-aware actions receive the correct cwd."""
        actions = build_action_set(cwd=str(tmp_path), env="local")
        for action in actions:
            if hasattr(action, "_cwd") or hasattr(action, "cwd"):
                cwd = getattr(action, "_cwd", None) or getattr(action, "cwd", None)
                assert str(tmp_path) in str(cwd)

    def test_returns_list_of_actions(self, tmp_path):
        """Returns a list, not an ActionRegistry."""
        from midas_agent.stdlib.action import Action

        actions = build_action_set(cwd=str(tmp_path), env="local")
        assert isinstance(actions, list)
        assert all(isinstance(a, Action) for a in actions)

    def test_at_least_seven_actions(self, tmp_path):
        """Local mode provides at least 7 actions (6 core + task_done)."""
        actions = build_action_set(cwd=str(tmp_path), env="local")
        assert len(actions) >= 7
