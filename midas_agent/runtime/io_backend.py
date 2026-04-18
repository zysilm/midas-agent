"""IO backend abstraction — eliminates Docker escaping bugs.

Provides a unified interface for file I/O and bash execution that works
identically in local (inference) and Docker (training) modes.

The key insight: Docker file writes go through `docker cp` via a temp
file instead of `docker exec printf '%s' '...'`, which corrupts backslashes.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from abc import ABC, abstractmethod


class IOBackend(ABC):
    """Abstract interface for file I/O and bash execution."""

    @abstractmethod
    def read_file(self, path: str) -> str:
        """Read the entire contents of a file."""
        ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write content to a file, creating parent dirs as needed."""
        ...

    @abstractmethod
    def run_bash(self, command: str, cwd: str | None = None, timeout: int = 120) -> str:
        """Execute a bash command and return combined output."""
        ...


class LocalIO(IOBackend):
    """Direct filesystem + subprocess. For inference/production."""

    def read_file(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def run_bash(self, command: str, cwd: str | None = None, timeout: int = 120) -> str:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += result.stderr
        return output


class DockerIO(IOBackend):
    """Routes all I/O through a Docker container.

    Key difference from the old Docker actions:
    - write_file uses `docker cp` via a temp file -- NO printf escaping
    - read_file uses `docker exec cat` (safe for reading)
    - run_bash uses `docker exec bash -c` (unchanged)
    """

    def __init__(self, container_id: str, workdir: str = "/testbed") -> None:
        self._cid = container_id
        self._workdir = workdir

    def read_file(self, path: str) -> str:
        result = subprocess.run(
            ["docker", "exec", self._cid, "cat", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise FileNotFoundError(f"File not found: {path}")
        return result.stdout

    def write_file(self, path: str, content: str) -> None:
        # Use docker cp via temp file -- NO printf escaping
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            subprocess.run(
                ["docker", "cp", tmp_path, f"{self._cid}:{path}"],
                check=True,
                capture_output=True,
                timeout=30,
            )
        finally:
            os.unlink(tmp_path)

    def run_bash(self, command: str, cwd: str | None = None, timeout: int = 120) -> str:
        workdir = cwd or self._workdir
        cmd = f"source activate testbed 2>/dev/null; {command}"
        result = subprocess.run(
            ["docker", "exec", "-w", workdir, self._cid, "bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += result.stderr
        return output
