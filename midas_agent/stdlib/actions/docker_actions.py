"""Docker-backed actions — all file/shell operations routed through a container.

Each class subclasses the corresponding local action and overrides only
execute().  name, description, and parameters are inherited unchanged,
so the LLM sees the exact same tool interface.

Usage:
    container_id = container_manager.start(image, ...)
    bash = DockerBashAction(container_id=cid)
    read = DockerReadFileAction(container_id=cid)
    ...
"""
from __future__ import annotations

import json
import subprocess
import shlex

from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.file_ops import (
    EditFileAction,
    ReadFileAction,
    WriteFileAction,
)
from midas_agent.stdlib.actions.search import FindFilesAction, SearchCodeAction


def _docker_exec(
    container_id: str,
    command: str,
    workdir: str = "/testbed",
    conda_env: str | None = "testbed",
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a command inside a Docker container and return the result."""
    if conda_env:
        command = f"source activate {conda_env} && {command}"
    return subprocess.run(
        [
            "docker", "exec",
            "-w", workdir,
            container_id,
            "bash", "-c", command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# DockerBashAction
# ---------------------------------------------------------------------------


class DockerBashAction(BashAction):
    """BashAction that executes commands inside a running Docker container."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
        conda_env: str | None = "testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir
        self._conda_env = conda_env

    def execute(self, **kwargs) -> str:
        command = kwargs["command"]
        timeout = kwargs.get("timeout", 120)
        try:
            result = _docker_exec(
                self._container_id, command,
                workdir=self._workdir,
                conda_env=self._conda_env,
                timeout=timeout,
            )
            output = result.stdout
            if result.returncode != 0 and result.stderr:
                output += result.stderr
            return output
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing command in container: {e}"


# ---------------------------------------------------------------------------
# DockerReadFileAction
# ---------------------------------------------------------------------------


class DockerReadFileAction(ReadFileAction):
    """ReadFileAction that reads files from inside the container."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def execute(self, **kwargs) -> str:
        path = kwargs["path"]
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit")

        # Resolve relative paths against workdir (inside container)
        if not path.startswith("/"):
            path = f"{self._workdir}/{path}"

        # Use awk for offset/limit to avoid reading entire large files
        if limit is not None:
            end = offset + limit
            cmd = f"awk 'NR>{offset} && NR<={end}' {shlex.quote(path)}"
        elif offset > 0:
            cmd = f"awk 'NR>{offset}' {shlex.quote(path)}"
        else:
            cmd = f"cat {shlex.quote(path)}"

        try:
            result = _docker_exec(
                self._container_id, cmd,
                workdir=self._workdir,
                conda_env=None,  # no conda needed for file ops
                timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "No such file" in stderr:
                    return f"File not found: {path}"
                return f"Error reading file: {stderr}"
            return result.stdout
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# DockerEditFileAction
# ---------------------------------------------------------------------------


class DockerEditFileAction(EditFileAction):
    """EditFileAction that edits files inside the container."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def _resolve_container_path(self, path: str) -> str:
        if not path.startswith("/"):
            return f"{self._workdir}/{path}"
        return path

    def execute(self, **kwargs) -> str:
        path = self._resolve_container_path(kwargs["path"])
        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        if old_string is None or new_string is None:
            return "Error: missing required parameter 'old_string' or 'new_string'"

        # Read current file content from container
        try:
            result = _docker_exec(
                self._container_id,
                f"cat {shlex.quote(path)}",
                workdir=self._workdir,
                conda_env=None,
                timeout=30,
            )
            if result.returncode != 0:
                return f"Error: file not found or cannot read: {path}"
            content = result.stdout
        except Exception as e:
            return f"Error reading file for edit: {e}"

        # Check occurrences of old_string
        count = content.count(old_string)
        if count == 0:
            return f"old_string not found in {path}"
        if count > 1:
            return (
                f"old_string is not unique in {path} "
                f"(found {count} occurrences). "
                f"Provide more surrounding context to make it unique."
            )

        # Exactly one occurrence — replace
        new_content = content.replace(old_string, new_string, 1)

        # Syntax check for Python files
        if path.endswith(".py"):
            import ast
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                return (
                    f"Syntax error in edited file — edit rejected. "
                    f"Original file unchanged.\n{e}"
                )

        # Write back to container
        try:
            # Use printf + redirect to handle special characters
            escaped = new_content.replace("\\", "\\\\").replace("'", "'\\''")
            write_cmd = f"printf '%s' '{escaped}' > {shlex.quote(path)}"
            result = _docker_exec(
                self._container_id, write_cmd,
                workdir=self._workdir,
                conda_env=None,
                timeout=30,
            )
            if result.returncode != 0:
                return f"Error writing file: {result.stderr}"
            return f"Edited {path}"
        except Exception as e:
            return f"Error writing edited file: {e}"


# ---------------------------------------------------------------------------
# DockerWriteFileAction
# ---------------------------------------------------------------------------


class DockerWriteFileAction(WriteFileAction):
    """WriteFileAction that writes files inside the container."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def execute(self, **kwargs) -> str:
        path = kwargs["path"]
        content = kwargs["content"]

        if not path.startswith("/"):
            path = f"{self._workdir}/{path}"

        try:
            # Create parent dirs
            parent = "/".join(path.rsplit("/", 1)[:-1])
            if parent:
                _docker_exec(
                    self._container_id,
                    f"mkdir -p {shlex.quote(parent)}",
                    workdir=self._workdir,
                    conda_env=None,
                    timeout=10,
                )

            # Write content using heredoc to handle special characters
            escaped = content.replace("\\", "\\\\").replace("'", "'\\''")
            write_cmd = f"printf '%s' '{escaped}' > {shlex.quote(path)}"
            result = _docker_exec(
                self._container_id, write_cmd,
                workdir=self._workdir,
                conda_env=None,
                timeout=30,
            )
            if result.returncode != 0:
                return f"Error writing file: {result.stderr}"
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# DockerSearchCodeAction
# ---------------------------------------------------------------------------


class DockerSearchCodeAction(SearchCodeAction):
    """SearchCodeAction that runs ripgrep inside the container."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def execute(self, **kwargs) -> str:
        pattern = kwargs["pattern"]
        path = kwargs.get("path", ".")
        include = kwargs.get("include")

        cmd_parts = ["grep", "-rnE", shlex.quote(pattern)]
        if include:
            cmd_parts.extend(["--include", shlex.quote(include)])
        cmd_parts.append(shlex.quote(path))

        try:
            result = _docker_exec(
                self._container_id,
                " ".join(cmd_parts),
                workdir=self._workdir,
                conda_env=None,
                timeout=30,
            )
            output = result.stdout.strip()
            if not output:
                return "No matches found"
            return output
        except Exception as e:
            return f"Error searching code: {e}"


# ---------------------------------------------------------------------------
# DockerFindFilesAction
# ---------------------------------------------------------------------------


class DockerFindFilesAction(FindFilesAction):
    """FindFilesAction that runs find inside the container."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def execute(self, **kwargs) -> str:
        pattern = kwargs["pattern"]
        path = kwargs.get("path", ".")

        # Convert glob pattern to find -name pattern
        # Handle **/ recursive patterns
        name_pattern = pattern.replace("**/", "")

        cmd = f"find {shlex.quote(path)} -name {shlex.quote(name_pattern)} -type f"

        try:
            result = _docker_exec(
                self._container_id,
                cmd,
                workdir=self._workdir,
                conda_env=None,
                timeout=30,
            )
            output = result.stdout.strip()
            if not output:
                return f"No files found matching: {pattern}"
            # Clean up leading ./
            lines = [l.lstrip("./") for l in output.split("\n") if l.strip()]
            return "\n".join(sorted(lines))
        except Exception as e:
            return f"Error finding files: {e}"
