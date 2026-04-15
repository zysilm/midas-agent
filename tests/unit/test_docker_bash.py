"""Unit tests for DockerBashAction."""
from unittest.mock import patch, MagicMock
import subprocess

import pytest

from midas_agent.stdlib.actions.docker_actions import DockerBashAction
from midas_agent.stdlib.actions.bash import BashAction


@pytest.mark.unit
class TestDockerBashAction:
    """Tests for DockerBashAction — bash execution inside Docker container."""

    def test_is_subclass_of_bash_action(self):
        """DockerBashAction is a BashAction subclass (drop-in replacement)."""
        action = DockerBashAction(container_id="abc123")
        assert isinstance(action, BashAction)

    def test_name_is_bash(self):
        """Tool name remains 'bash' — LLM sees the same tool."""
        action = DockerBashAction(container_id="abc123")
        assert action.name == "bash"

    def test_description_exists(self):
        """Description is inherited from BashAction."""
        action = DockerBashAction(container_id="abc123")
        assert len(action.description) > 0

    @patch("midas_agent.stdlib.actions.docker_actions.subprocess.run")
    def test_execute_calls_docker_exec(self, mock_run):
        """execute() routes command through docker exec."""
        mock_run.return_value = MagicMock(
            stdout="hello world\n", stderr="", returncode=0,
        )

        action = DockerBashAction(container_id="abc123", workdir="/workspace")
        result = action.execute(command="echo hello")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert args[1] == "exec"
        assert "-w" in args
        assert "/workspace" in args
        assert "abc123" in args
        assert "bash" in args
        assert "echo hello" in args[-1] or "echo hello" in " ".join(args)
        assert result == "hello world\n"

    @patch("midas_agent.stdlib.actions.docker_actions.subprocess.run")
    def test_execute_returns_stderr_on_failure(self, mock_run):
        """When command fails, stderr is appended to output."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="error: not found\n", returncode=1,
        )

        action = DockerBashAction(container_id="abc123")
        result = action.execute(command="bad_cmd")

        assert "error: not found" in result

    @patch("midas_agent.stdlib.actions.docker_actions.subprocess.run")
    def test_execute_handles_timeout(self, mock_run):
        """Timeout returns a meaningful message, not a crash."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=120)

        action = DockerBashAction(container_id="abc123")
        result = action.execute(command="sleep 999", timeout=120)

        assert "timed out" in result.lower()

    @patch("midas_agent.stdlib.actions.docker_actions.subprocess.run")
    def test_custom_workdir(self, mock_run):
        """workdir parameter is passed as -w to docker exec."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        action = DockerBashAction(
            container_id="cid", workdir="/custom/path",
        )
        action.execute(command="ls")

        args = mock_run.call_args[0][0]
        w_idx = args.index("-w")
        assert args[w_idx + 1] == "/custom/path"

    @patch("midas_agent.stdlib.actions.docker_actions.subprocess.run")
    def test_timeout_passed_to_subprocess(self, mock_run):
        """Custom timeout is forwarded to subprocess.run."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

        action = DockerBashAction(container_id="cid")
        action.execute(command="ls", timeout=30)

        assert mock_run.call_args[1]["timeout"] == 30
