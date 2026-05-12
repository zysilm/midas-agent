"""Unit tests for --issue-index CLI feature.

Tests define the target behavior for selecting a single SWE-bench issue
by its index. Tests are expected to FAIL until the feature is implemented.

Feature spec:
  midas train --config train.yaml --issue-index 2
  → loads all SWE-bench issues, picks issues[2], trains on that single issue.

Design constraints:
  - --issue-index and --issues are mutually exclusive.
  - Index is 0-based (Python convention).
  - Out-of-range index → clear error, not silent empty list.
  - Negative index → rejected by argparse (non-negative int).
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.cli import parse_args
from llm_agent_toolkit.types import Issue


# ===================================================================
# Helpers
# ===================================================================


def _fake_issues(n: int = 5) -> list[Issue]:
    """Build a list of N distinguishable fake issues."""
    return [
        Issue(
            issue_id=f"repo__project-{i}",
            repo="repo/project",
            description=f"Fix bug number {i}",
            base_commit=f"abc{i:04d}",
            fail_to_pass=[f"tests/test_{i}.py::test_fail"],
            pass_to_pass=[f"tests/test_{i}.py::test_pass"],
        )
        for i in range(n)
    ]


def _write_config_yaml(path: str) -> None:
    """Write a minimal valid training config YAML."""
    import yaml

    config = {
        "initial_budget": 1000,
        "workspace_count": 1,
        "runtime_mode": "graph_emergence",
    }
    with open(path, "w") as f:
        yaml.dump(config, f)


# ===================================================================
# 1. CLI argument parsing
# ===================================================================


@pytest.mark.unit
class TestIssueIndexArgParsing:
    """--issue-index is accepted, parsed as int, defaults to None."""

    def test_issue_index_parsed_as_int(self):
        """--issue-index 2 sets args.issue_index to 2."""
        args = parse_args(["train", "--config", "train.yaml", "--issue-index", "2"])
        assert args.issue_index == 2

    def test_issue_index_zero(self):
        """--issue-index 0 selects the first issue."""
        args = parse_args(["train", "--config", "train.yaml", "--issue-index", "0"])
        assert args.issue_index == 0

    def test_issue_index_defaults_to_none(self):
        """Omitting --issue-index leaves it as None."""
        args = parse_args(["train", "--config", "train.yaml"])
        assert args.issue_index is None

    def test_issue_index_not_available_on_infer(self):
        """--issue-index is a train-only flag, not available on infer."""
        with pytest.raises(SystemExit):
            parse_args(["infer", "--issue-index", "0"])

    def test_issue_index_requires_integer(self):
        """--issue-index abc → argparse error (not a valid int)."""
        with pytest.raises(SystemExit):
            parse_args(["train", "--config", "t.yaml", "--issue-index", "abc"])

    def test_issue_index_rejects_negative(self):
        """--issue-index -1 → rejected (negative index not allowed)."""
        with pytest.raises(SystemExit):
            parse_args(["train", "--config", "t.yaml", "--issue-index", "-1"])


# ===================================================================
# 2. Mutual exclusion with --issues
# ===================================================================


@pytest.mark.unit
class TestIssueIndexMutualExclusion:
    """--issue-index and --issues cannot be used together."""

    def test_both_flags_rejected(self):
        """Using --issues and --issue-index together → error."""
        with pytest.raises(SystemExit):
            parse_args([
                "train", "--config", "t.yaml",
                "--issues", "5",
                "--issue-index", "2",
            ])

    def test_issues_alone_still_works(self):
        """--issues without --issue-index works as before (no regression)."""
        args = parse_args(["train", "--config", "t.yaml", "--issues", "10"])
        assert args.issues == 10
        assert args.issue_index is None

    def test_issue_index_alone_works(self):
        """--issue-index without --issues works."""
        args = parse_args(["train", "--config", "t.yaml", "--issue-index", "3"])
        assert args.issue_index == 3
        assert args.issues is None


# ===================================================================
# 3. Issue selection logic in _cmd_train
# ===================================================================


@pytest.mark.unit
class TestIssueIndexSelection:
    """_cmd_train selects the correct single issue and passes it to run_training."""

    @patch("midas_agent.training.run_training")
    @patch("midas_agent.training.load_swe_bench", return_value=_fake_issues(5))
    @patch("midas_agent.resolver.resolve_llm_config")
    def test_index_selects_correct_issue(
        self, mock_resolve, mock_load, mock_train, tmp_path,
    ):
        """--issue-index 2 passes issues[2] (third issue) to run_training."""
        from midas_agent.resolver import LLMConfig

        mock_resolve.return_value = LLMConfig(model="test", api_key="k", api_base="")

        config_path = str(tmp_path / "train.yaml")
        _write_config_yaml(config_path)

        from midas_agent.cli import _cmd_train

        args = parse_args(["train", "--config", config_path, "--issue-index", "2"])
        _cmd_train(args)

        mock_train.assert_called_once()
        issues_passed = mock_train.call_args[1].get("issues") or mock_train.call_args[0][1]
        assert len(issues_passed) == 1
        assert issues_passed[0].issue_id == "repo__project-2"

    @patch("midas_agent.training.run_training")
    @patch("midas_agent.training.load_swe_bench", return_value=_fake_issues(5))
    @patch("midas_agent.resolver.resolve_llm_config")
    def test_index_zero_selects_first_issue(
        self, mock_resolve, mock_load, mock_train, tmp_path,
    ):
        """--issue-index 0 passes issues[0] to run_training."""
        from midas_agent.resolver import LLMConfig

        mock_resolve.return_value = LLMConfig(model="test", api_key="k", api_base="")

        config_path = str(tmp_path / "train.yaml")
        _write_config_yaml(config_path)

        from midas_agent.cli import _cmd_train

        args = parse_args(["train", "--config", config_path, "--issue-index", "0"])
        _cmd_train(args)

        mock_train.assert_called_once()
        issues_passed = mock_train.call_args[1].get("issues") or mock_train.call_args[0][1]
        assert len(issues_passed) == 1
        assert issues_passed[0].issue_id == "repo__project-0"

    @patch("midas_agent.training.run_training")
    @patch("midas_agent.training.load_swe_bench", return_value=_fake_issues(5))
    @patch("midas_agent.resolver.resolve_llm_config")
    def test_index_last_valid(
        self, mock_resolve, mock_load, mock_train, tmp_path,
    ):
        """--issue-index 4 (last index for 5 issues) works."""
        from midas_agent.resolver import LLMConfig

        mock_resolve.return_value = LLMConfig(model="test", api_key="k", api_base="")

        config_path = str(tmp_path / "train.yaml")
        _write_config_yaml(config_path)

        from midas_agent.cli import _cmd_train

        args = parse_args(["train", "--config", config_path, "--issue-index", "4"])
        _cmd_train(args)

        mock_train.assert_called_once()
        issues_passed = mock_train.call_args[1].get("issues") or mock_train.call_args[0][1]
        assert len(issues_passed) == 1
        assert issues_passed[0].issue_id == "repo__project-4"

    @patch("midas_agent.training.load_swe_bench", return_value=_fake_issues(5))
    @patch("midas_agent.resolver.resolve_llm_config")
    def test_index_out_of_range_exits(
        self, mock_resolve, mock_load, tmp_path,
    ):
        """--issue-index 10 with only 5 issues → sys.exit with error."""
        from midas_agent.resolver import LLMConfig

        mock_resolve.return_value = LLMConfig(model="test", api_key="k", api_base="")

        config_path = str(tmp_path / "train.yaml")
        _write_config_yaml(config_path)

        from midas_agent.cli import _cmd_train

        args = parse_args(["train", "--config", config_path, "--issue-index", "10"])

        with pytest.raises(SystemExit):
            _cmd_train(args)


# ===================================================================
# 4. No regression for existing --issues flag
# ===================================================================


@pytest.mark.unit
class TestIssuesSliceRegression:
    """Existing --issues N behavior is unchanged."""

    @patch("midas_agent.training.run_training")
    @patch("midas_agent.training.load_swe_bench", return_value=_fake_issues(10))
    @patch("midas_agent.resolver.resolve_llm_config")
    def test_issues_flag_slices_first_n(
        self, mock_resolve, mock_load, mock_train, tmp_path,
    ):
        """--issues 3 passes the first 3 issues."""
        from midas_agent.resolver import LLMConfig

        mock_resolve.return_value = LLMConfig(model="test", api_key="k", api_base="")

        config_path = str(tmp_path / "train.yaml")
        _write_config_yaml(config_path)

        from midas_agent.cli import _cmd_train

        args = parse_args(["train", "--config", config_path, "--issues", "3"])
        _cmd_train(args)

        mock_train.assert_called_once()
        issues_passed = mock_train.call_args[1].get("issues") or mock_train.call_args[0][1]
        assert len(issues_passed) == 3
        assert issues_passed[0].issue_id == "repo__project-0"
        assert issues_passed[2].issue_id == "repo__project-2"

    @patch("midas_agent.training.run_training")
    @patch("midas_agent.training.load_swe_bench", return_value=_fake_issues(10))
    @patch("midas_agent.resolver.resolve_llm_config")
    def test_no_flags_passes_all_issues(
        self, mock_resolve, mock_load, mock_train, tmp_path,
    ):
        """No --issues or --issue-index passes all issues."""
        from midas_agent.resolver import LLMConfig

        mock_resolve.return_value = LLMConfig(model="test", api_key="k", api_base="")

        config_path = str(tmp_path / "train.yaml")
        _write_config_yaml(config_path)

        from midas_agent.cli import _cmd_train

        args = parse_args(["train", "--config", config_path])
        _cmd_train(args)

        mock_train.assert_called_once()
        issues_passed = mock_train.call_args[1].get("issues") or mock_train.call_args[0][1]
        assert len(issues_passed) == 10
