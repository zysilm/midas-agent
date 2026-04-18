"""Docker-backed actions — all file/shell operations routed through a container.

Each class subclasses the corresponding local action and overrides only
execute().  name, description, and parameters are inherited unchanged,
so the LLM sees the exact same tool interface.

Usage:
    container_id = container_manager.start(image, ...)
    bash = DockerBashAction(container_id=cid)
    editor = DockerStrReplaceEditorAction(container_id=cid)
    ...
"""
from __future__ import annotations

import ast
import json
import subprocess
import shlex

from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.file_ops import (
    EditFileAction,
    ReadFileAction,
    WriteFileAction,
)
from midas_agent.stdlib.actions.str_replace_editor import (
    SNIPPET_LINES,
    StrReplaceEditorAction,
    _make_output,
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

            # Show context snippet around the edit
            snippet = self._snippet(new_content, new_string, path)
            return snippet
        except Exception as e:
            return f"Error writing edited file: {e}"

    @staticmethod
    def _snippet(content: str, new_string: str, path: str, context_lines: int = 4) -> str:
        """Return cat -n style snippet around the edited region."""
        lines = content.splitlines()
        # Find the start of new_string in the content
        idx = content.find(new_string)
        if idx == -1:
            return f"Edited {path}"
        line_num = content[:idx].count("\n")
        start = max(0, line_num - context_lines)
        end = min(len(lines), line_num + new_string.count("\n") + 1 + context_lines)
        snippet_lines = []
        for i in range(start, end):
            snippet_lines.append(f"  {i + 1:>5}\t{lines[i]}")
        return (
            f"The file {path} has been edited. Here's the result of running `cat -n` on a snippet:\n"
            + "\n".join(snippet_lines)
        )


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
# DockerStrReplaceEditorAction
# ---------------------------------------------------------------------------


class DockerStrReplaceEditorAction(StrReplaceEditorAction):
    """StrReplaceEditorAction that routes all file operations through Docker."""

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/testbed",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def _resolve(self, path: str) -> str:
        """Resolve path inside the container."""
        if path.startswith("/"):
            return path
        return f"{self._workdir}/{path}"

    def _docker_read(self, path: str) -> str:
        """Read a file from inside the container."""
        result = _docker_exec(
            self._container_id,
            f"cat {shlex.quote(path)}",
            workdir=self._workdir,
            conda_env=None,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No such file" in stderr:
                raise FileNotFoundError(path)
            raise RuntimeError(stderr)
        return result.stdout

    def _docker_write(self, path: str, content: str) -> None:
        """Write content to a file inside the container."""
        escaped = content.replace("\\", "\\\\").replace("'", "'\\''")
        write_cmd = f"printf '%s' '{escaped}' > {shlex.quote(path)}"
        result = _docker_exec(
            self._container_id, write_cmd,
            workdir=self._workdir,
            conda_env=None,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

    def _docker_exists(self, path: str) -> bool:
        """Check if a path exists inside the container."""
        result = _docker_exec(
            self._container_id,
            f"test -e {shlex.quote(path)} && echo yes || echo no",
            workdir=self._workdir,
            conda_env=None,
            timeout=10,
        )
        return result.stdout.strip() == "yes"

    def _docker_isdir(self, path: str) -> bool:
        """Check if a path is a directory inside the container."""
        result = _docker_exec(
            self._container_id,
            f"test -d {shlex.quote(path)} && echo yes || echo no",
            workdir=self._workdir,
            conda_env=None,
            timeout=10,
        )
        return result.stdout.strip() == "yes"

    def _view(self, path: str, view_range: list[int] | None) -> str:
        if not self._docker_exists(path):
            return f"Error: the path {path} does not exist. Please provide a valid path."

        if self._docker_isdir(path):
            if view_range:
                return "Error: the `view_range` parameter is not allowed when `path` points to a directory."
            # List directory inside container
            result = _docker_exec(
                self._container_id,
                f"find {shlex.quote(path)} -maxdepth 2 -not -path '*/.*' | sort",
                workdir=self._workdir,
                conda_env=None,
                timeout=30,
            )
            listing = result.stdout.strip()
            return (
                f"Here's the files and directories up to 2 levels deep in {path}, "
                f"excluding hidden items:\n{listing}\n"
            )

        try:
            content = self._docker_read(path)
        except Exception as e:
            return f"Error reading file: {e}"

        if view_range:
            if len(view_range) != 2 or not all(isinstance(i, int) for i in view_range):
                return "Error: invalid `view_range`. It should be a list of two integers."
            file_lines = content.split("\n")
            n_lines = len(file_lines)
            init_line, final_line = view_range
            if init_line < 1 or init_line > n_lines:
                return (
                    f"Error: invalid `view_range`: {view_range}. "
                    f"First element `{init_line}` should be within [1, {n_lines}]."
                )
            if final_line != -1 and final_line > n_lines:
                return (
                    f"Error: invalid `view_range`: {view_range}. "
                    f"Second element `{final_line}` should be <= {n_lines}."
                )
            if final_line != -1 and final_line < init_line:
                return (
                    f"Error: invalid `view_range`: {view_range}. "
                    f"Second element `{final_line}` should be >= first element `{init_line}`."
                )
            if final_line == -1:
                final_line = n_lines
            content = "\n".join(file_lines[init_line - 1 : final_line])
            return _make_output(content, str(path), init_line=init_line)

        return _make_output(content, str(path), init_line=1)

    def _create(self, path: str, file_text: str) -> str:
        if self._docker_exists(path):
            return f"Error: file already exists at: {path}. Cannot overwrite files using command `create`."

        # Check parent directory exists
        parent = "/".join(path.rsplit("/", 1)[:-1])
        if parent and not self._docker_exists(parent):
            return f"Error: the parent directory {parent} does not exist. Please create it first."

        try:
            self._docker_write(path, file_text)
        except Exception as e:
            return f"Error writing file: {e}"

        self._undo_history.setdefault(path, []).append(file_text)
        return f"File created successfully at: {path}"

    def _str_replace(self, path: str, old_str: str, new_str: str | None) -> str:
        try:
            content = self._docker_read(path)
        except FileNotFoundError:
            return f"Error: file not found: {path}"
        except Exception as e:
            return f"Error reading file: {e}"

        if new_str is None:
            new_str = ""

        count = content.count(old_str)
        if count == 0:
            return (
                f"No replacement was performed, old_str `{old_str}` did not appear "
                f"verbatim in {path}."
            )
        if count > 1:
            return (
                f"No replacement was performed. Multiple occurrences of old_str "
                f"`{old_str}` in {path} (found {count} occurrences). "
                f"Please ensure it is unique."
            )

        new_content = content.replace(old_str, new_str, 1)

        if path.endswith(".py"):
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                return (
                    "<SYNTAX_ERROR>\n"
                    "Your edit introduced a syntax error. "
                    "Edit rejected; file unchanged:\n"
                    f"{e}\n"
                    "</SYNTAX_ERROR>"
                )

        self._undo_history.setdefault(path, []).append(content)

        try:
            self._docker_write(path, new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        replacement_line = content.split(old_str)[0].count("\n")
        start_line = max(1, replacement_line - SNIPPET_LINES + 1)
        end_line = min(
            replacement_line + SNIPPET_LINES + new_str.count("\n") + 1,
            len(new_content.splitlines()),
        )
        snippet = "\n".join(new_content.splitlines()[start_line - 1 : end_line])

        success_msg = f"The file {path} has been edited. "
        success_msg += _make_output(snippet, f"a snippet of {path}", start_line)
        success_msg += "Review the changes and make sure they are as expected. Edit the file again if necessary."
        return success_msg

    def _insert(self, path: str, insert_line: int, new_str: str) -> str:
        try:
            content = self._docker_read(path)
        except FileNotFoundError:
            return f"Error: file not found: {path}"
        except Exception as e:
            return f"Error reading file: {e}"

        file_lines = content.split("\n")
        n_lines = len(file_lines)

        if insert_line < 0 or insert_line > n_lines:
            return (
                f"Error: invalid `insert_line` parameter: {insert_line}. "
                f"It should be within [0, {n_lines}]."
            )

        new_str_lines = new_str.split("\n")
        new_file_lines = (
            file_lines[:insert_line] + new_str_lines + file_lines[insert_line:]
        )
        new_content = "\n".join(new_file_lines)

        self._undo_history.setdefault(path, []).append(content)

        try:
            self._docker_write(path, new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        snippet_lines = (
            file_lines[max(0, insert_line - SNIPPET_LINES) : insert_line]
            + new_str_lines
            + file_lines[insert_line : insert_line + SNIPPET_LINES]
        )
        snippet = "\n".join(snippet_lines)

        success_msg = f"The file {path} has been edited. "
        success_msg += _make_output(
            snippet,
            "a snippet of the edited file",
            max(1, insert_line - SNIPPET_LINES + 1),
        )
        success_msg += (
            "Review the changes and make sure they are as expected "
            "(correct indentation, no duplicate lines, etc). "
            "Edit the file again if necessary."
        )
        return success_msg

    def _undo_edit(self, path: str) -> str:
        history = self._undo_history.get(path, [])
        if not history:
            return f"Error: no edit history found for {path}."

        old_text = history.pop()
        try:
            self._docker_write(path, old_text)
        except Exception as e:
            return f"Error reverting file: {e}"

        return (
            f"Last edit to {path} undone successfully. "
            + _make_output(old_text, str(path))
        )


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
