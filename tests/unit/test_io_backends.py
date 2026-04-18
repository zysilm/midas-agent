"""Unit tests for IO backends (LocalIO and DockerIO).

Tests define the target behavior of:
- IOBackend: abstract interface for file I/O and bash execution
- LocalIO: direct filesystem + subprocess (for inference/production)
- DockerIO: routes through Docker container (for training)
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

from midas_agent.runtime.io_backend import IOBackend, LocalIO, DockerIO


# ===================================================================
# IOBackend ABC
# ===================================================================


@pytest.mark.unit
class TestIOBackendInterface:
    """IOBackend is an abstract base class with required methods."""

    def test_cannot_instantiate_directly(self):
        """IOBackend cannot be instantiated — it's abstract."""
        with pytest.raises(TypeError):
            IOBackend()

    def test_has_read_file_method(self):
        """IOBackend declares read_file as abstract."""
        assert hasattr(IOBackend, "read_file")

    def test_has_write_file_method(self):
        """IOBackend declares write_file as abstract."""
        assert hasattr(IOBackend, "write_file")

    def test_has_run_bash_method(self):
        """IOBackend declares run_bash as abstract."""
        assert hasattr(IOBackend, "run_bash")


# ===================================================================
# LocalIO
# ===================================================================


@pytest.mark.unit
class TestLocalIO:
    """LocalIO uses direct filesystem and subprocess."""

    def test_is_io_backend(self):
        """LocalIO is a subclass of IOBackend."""
        io = LocalIO()
        assert isinstance(io, IOBackend)

    def test_read_file(self, tmp_path):
        """read_file reads content from disk."""
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        io = LocalIO()
        assert io.read_file(str(f)) == "hello world"

    def test_write_file(self, tmp_path):
        """write_file writes content to disk."""
        f = tmp_path / "test.txt"
        io = LocalIO()
        io.write_file(str(f), "hello world")
        assert f.read_text() == "hello world"

    def test_write_file_creates_parent_dirs(self, tmp_path):
        """write_file creates parent directories if needed."""
        f = tmp_path / "deep" / "nested" / "test.txt"
        io = LocalIO()
        io.write_file(str(f), "nested content")
        assert f.read_text() == "nested content"

    def test_run_bash(self):
        """run_bash executes a shell command and returns output."""
        io = LocalIO()
        result = io.run_bash("echo hello")
        assert "hello" in result

    def test_run_bash_with_cwd(self, tmp_path):
        """run_bash respects the cwd parameter."""
        io = LocalIO()
        result = io.run_bash("pwd", cwd=str(tmp_path))
        assert str(tmp_path) in result

    def test_run_bash_returns_stderr_on_failure(self):
        """run_bash includes stderr when command fails."""
        io = LocalIO()
        result = io.run_bash("ls /nonexistent_path_xyz_123")
        assert result  # Should have some error output

    def test_backslash_roundtrip(self, tmp_path):
        """Write content with backslashes and read it back byte-for-byte."""
        f = tmp_path / "regex.py"
        content = r're.compile(r"(\W|\b|_)")'
        io = LocalIO()
        io.write_file(str(f), content)
        assert io.read_file(str(f)) == content

    def test_complex_backslash_content(self, tmp_path):
        """Complex regex patterns with backslashes survive write/read."""
        f = tmp_path / "complex.py"
        content = 'pattern = re.compile(r"(\\W|\\b|_)")\nother = r"\\n\\t\\r"'
        io = LocalIO()
        io.write_file(str(f), content)
        assert io.read_file(str(f)) == content


# ===================================================================
# DockerIO
# ===================================================================


@pytest.mark.unit
class TestDockerIO:
    """DockerIO routes all operations through Docker container."""

    def test_is_io_backend(self):
        """DockerIO is a subclass of IOBackend."""
        io = DockerIO(container_id="abc123")
        assert isinstance(io, IOBackend)

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_read_file(self, mock_run):
        """read_file uses docker exec cat."""
        mock_run.return_value = MagicMock(
            stdout="file content", stderr="", returncode=0,
        )
        io = DockerIO(container_id="abc123")
        result = io.read_file("/testbed/file.py")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert args[1] == "exec"
        assert "abc123" in args
        assert "cat" in args
        assert "/testbed/file.py" in args
        assert result == "file content"

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_write_file_uses_docker_cp(self, mock_run):
        """write_file uses docker cp via temp file -- no printf escaping."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="", returncode=0,
        )
        io = DockerIO(container_id="abc123")
        io.write_file("/testbed/file.py", "content")

        # Should call docker cp (not docker exec with printf)
        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert args[1] == "cp"
        assert "abc123:/testbed/file.py" in args[-1] or f"abc123:/testbed/file.py" in " ".join(args)

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_write_file_no_printf_escaping(self, mock_run):
        """write_file must NOT use printf escaping -- the key fix."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="", returncode=0,
        )
        io = DockerIO(container_id="abc123")
        content = r're.compile(r"(\W|\b|_)")'
        io.write_file("/testbed/file.py", content)

        # The content should be written to a temp file, not passed through bash
        call_args_str = str(mock_run.call_args_list)
        assert "printf" not in call_args_str
        assert "\\\\W" not in call_args_str  # No double-escaping

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_run_bash(self, mock_run):
        """run_bash uses docker exec with bash -c."""
        mock_run.return_value = MagicMock(
            stdout="hello\n", stderr="", returncode=0,
        )
        io = DockerIO(container_id="abc123", workdir="/testbed")
        result = io.run_bash("echo hello")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert args[1] == "exec"
        assert "-w" in args
        assert "/testbed" in args
        assert "abc123" in args
        assert "bash" in args
        assert result == "hello\n"

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_run_bash_custom_cwd(self, mock_run):
        """run_bash uses cwd parameter when provided."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        io = DockerIO(container_id="abc123", workdir="/testbed")
        io.run_bash("ls", cwd="/custom/path")

        args = mock_run.call_args[0][0]
        w_idx = args.index("-w")
        assert args[w_idx + 1] == "/custom/path"

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_run_bash_returns_stderr_on_failure(self, mock_run):
        """When command fails, stderr is appended."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="error: not found\n", returncode=1,
        )
        io = DockerIO(container_id="abc123")
        result = io.run_bash("bad_cmd")

        assert "error: not found" in result

    @patch("midas_agent.runtime.io_backend.subprocess.run")
    def test_backslash_safety_via_docker_cp(self, mock_run):
        """Backslash content survives write via docker cp."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="", returncode=0,
        )
        io = DockerIO(container_id="abc123")
        content = r're.compile(r"(\W|\b|_)")'
        io.write_file("/testbed/file.py", content)

        # Verify no double-escaping happened
        # The content is written to a temp file and docker cp'd
        # so it should be byte-for-byte identical
        call_args = mock_run.call_args_list
        assert len(call_args) >= 1
        docker_cp_call = call_args[-1]  # last call should be docker cp
        assert "docker" in docker_cp_call[0][0][0]
        assert "cp" in docker_cp_call[0][0][1]
