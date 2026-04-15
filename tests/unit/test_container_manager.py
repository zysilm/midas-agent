"""Unit tests for ContainerManager."""
from unittest.mock import patch, MagicMock, call
import subprocess

import pytest

from midas_agent.docker.container_manager import ContainerManager


@pytest.mark.unit
class TestContainerManager:
    """Tests for ContainerManager — Docker container lifecycle."""

    def test_construction(self):
        """ContainerManager starts with no container."""
        cm = ContainerManager()
        assert cm.container_id is None

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_start_pulls_and_runs(self, mock_run):
        """start() pulls the image if needed, then runs a container."""
        # First call: docker image inspect → not found (returncode=1)
        # Second call: docker pull → success
        # Third call: docker run → returns container ID
        # Fourth call: docker exec (pip install) → success
        mock_run.side_effect = [
            MagicMock(returncode=1),  # inspect: not found
            MagicMock(returncode=0, stderr=""),  # pull: ok
            MagicMock(returncode=0, stdout="abcdef123456\n", stderr=""),  # run
            MagicMock(returncode=0, stdout="", stderr=""),  # pip install
        ]

        cm = ContainerManager()
        cid = cm.start(
            image="swebench/sweb.eval.x86_64.test:latest",
            host_workspace="/tmp/workspace",
        )

        assert cid == "abcdef123456"
        assert cm.container_id == "abcdef123456"
        assert mock_run.call_count == 4

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_start_skips_pull_if_image_exists(self, mock_run):
        """start() skips pull when image is already available locally."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # inspect: found
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # run
            MagicMock(returncode=0, stdout="", stderr=""),  # pip install
        ]

        cm = ContainerManager()
        cid = cm.start(image="existing:latest", host_workspace="/tmp/ws")

        assert cid is not None
        # No pull call (inspect succeeded)
        assert mock_run.call_count == 3

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_start_without_install(self, mock_run):
        """start() with install_cmd=None skips pip install."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # inspect: found
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # run
        ]

        cm = ContainerManager()
        cm.start(image="img:latest", host_workspace="/tmp/ws", install_cmd=None)

        # Only inspect + run, no exec
        assert mock_run.call_count == 2

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_start_mounts_volume(self, mock_run):
        """start() passes -v host_workspace:/workspace to docker run."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # inspect
            MagicMock(returncode=0, stdout="cid\n", stderr=""),  # run
            MagicMock(returncode=0, stdout="", stderr=""),  # install
        ]

        cm = ContainerManager()
        cm.start(image="img:latest", host_workspace="/my/repo")

        run_call = mock_run.call_args_list[1]
        args = run_call[0][0]
        assert "-v" in args
        v_idx = args.index("-v")
        assert "/my/repo:/workspace" == args[v_idx + 1]

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_stop_removes_container(self, mock_run):
        """stop() calls docker rm -f with the container ID."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # inspect
            MagicMock(returncode=0, stdout="cid123\n", stderr=""),  # run
            MagicMock(returncode=0, stdout="", stderr=""),  # install
            MagicMock(returncode=0),  # rm
        ]

        cm = ContainerManager()
        cm.start(image="img:latest", host_workspace="/tmp/ws")
        assert cm.container_id is not None

        cm.stop()

        rm_call = mock_run.call_args_list[3]
        args = rm_call[0][0]
        assert "docker" in args
        assert "rm" in args
        assert "-f" in args
        assert cm.container_id is None

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_stop_without_start_is_noop(self, mock_run):
        """stop() without start() does nothing."""
        cm = ContainerManager()
        cm.stop()

        mock_run.assert_not_called()

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_start_raises_on_run_failure(self, mock_run):
        """start() raises RuntimeError if docker run fails."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # inspect
            MagicMock(returncode=1, stderr="no space left"),  # run fails
        ]

        cm = ContainerManager()
        with pytest.raises(RuntimeError, match="no space left"):
            cm.start(image="img:latest", host_workspace="/tmp/ws")

    @patch("midas_agent.docker.container_manager.subprocess.run")
    def test_start_raises_on_pull_failure(self, mock_run):
        """start() raises RuntimeError if docker pull fails."""
        mock_run.side_effect = [
            MagicMock(returncode=1),  # inspect: not found
            MagicMock(returncode=1, stderr="auth required"),  # pull fails
        ]

        cm = ContainerManager()
        with pytest.raises(RuntimeError, match="auth required"):
            cm.start(image="img:latest", host_workspace="/tmp/ws")
