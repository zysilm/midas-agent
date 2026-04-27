"""Unified str_replace_editor action — SWE-agent compatible interface.

Replaces the separate read_file, edit_file, and write_file actions with a
single tool matching SWE-agent's str_replace_editor interface.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from midas_agent.stdlib.action import Action

if TYPE_CHECKING:
    from midas_agent.runtime.io_backend import IOBackend

SNIPPET_LINES: int = 4
TRUNCATED_MESSAGE: str = (
    "<response clipped><NOTE>To save on context only part of this file has been "
    "shown to you. You should retry this tool after you have searched inside the "
    "file with `grep -n` in order to find the line numbers of what you are "
    "looking for.</NOTE>"
)
MAX_RESPONSE_LEN: int = 16000


def _maybe_truncate(content: str, max_len: int = MAX_RESPONSE_LEN) -> str:
    if len(content) <= max_len:
        return content
    return content[:max_len] + TRUNCATED_MESSAGE


def _make_output(
    file_content: str,
    file_descriptor: str,
    init_line: int = 1,
) -> str:
    """Generate cat -n style output."""
    file_content = _maybe_truncate(file_content)
    file_content = file_content.expandtabs()
    numbered = "\n".join(
        f"{i + init_line:6}\t{line}"
        for i, line in enumerate(file_content.split("\n"))
    )
    return (
        f"Here's the result of running `cat -n` on {file_descriptor}:\n"
        + numbered
        + "\n"
    )


class StrReplaceEditorAction(Action):
    """Unified file editor matching SWE-agent's str_replace_editor interface."""

    # Shared undo history across all instances (class-level)
    _undo_history: dict[str, list[str]] = {}

    def __init__(self, cwd: str | None = None, io: IOBackend | None = None) -> None:
        self.cwd = cwd
        self._io = io

    @property
    def name(self) -> str:
        return "str_replace_editor"

    @property
    def description(self) -> str:
        return (
            "Custom editing tool for viewing, creating and editing files\n"
            "* State is persistent across command calls and discussions with the user\n"
            "* If `path` is a file, `view` displays the result of applying `cat -n`. "
            "If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep\n"
            "* The `create` command cannot be used if the specified `path` already exists as a file\n"
            "* If a `command` generates a long output, it will be truncated and marked with `<response clipped>`\n"
            "* The `undo_edit` command will revert the last edit made to the file at `path`\n"
            "\n"
            "Notes for using the `str_replace` command:\n"
            "* The `old_str` parameter should match EXACTLY one or more consecutive lines from the original file. "
            "Be mindful of whitespaces!\n"
            "* If the `old_str` parameter is not unique in the file, the replacement will not be performed. "
            "Make sure to include enough context in `old_str` to make it unique\n"
            "* The `new_str` parameter should contain the edited lines that should replace the `old_str`"
        )

    @property
    def parameters(self) -> dict:
        return {
            "command": {
                "type": "string",
                "required": True,
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                "description": "The commands to run. Allowed options are: `view`, `create`, `str_replace`, `insert`, `undo_edit`.",
            },
            "path": {
                "type": "string",
                "required": True,
                "description": "Absolute path to file or directory, e.g. `/testbed/file.py` or `/testbed`.",
            },
            "file_text": {
                "type": "string",
                "required": False,
                "description": "Required parameter of `create` command, with the content of the file to be created.",
            },
            "old_str": {
                "type": "string",
                "required": False,
                "description": "Required parameter of `str_replace` command containing the string in `path` to replace.",
            },
            "new_str": {
                "type": "string",
                "required": False,
                "description": (
                    "Optional parameter of `str_replace` command containing the new string "
                    "(if not given, no string will be added). Required parameter of `insert` "
                    "command containing the string to insert."
                ),
            },
            "insert_line": {
                "type": "integer",
                "required": False,
                "description": "Required parameter of `insert` command. The `new_str` will be inserted AFTER the line `insert_line` of `path`.",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "required": False,
                "description": (
                    "Optional parameter of `view` command when `path` points to a file. "
                    "If none is given, the full file is shown. If provided, the file will be shown "
                    "in the indicated line number range, e.g. [11, 12] will show lines 11 and 12. "
                    "Indexing at 1 to start. Setting `[start_line, -1]` shows all lines from "
                    "`start_line` to the end of the file."
                ),
            },
        }

    def _resolve(self, path: str) -> str:
        """Resolve path; if absolute use as-is, else join with cwd."""
        if os.path.isabs(path):
            return path
        if self.cwd:
            return os.path.join(self.cwd, path)
        return path

    def execute(self, **kwargs) -> str:
        command = kwargs.get("command")
        path_raw = kwargs.get("path")

        if not command:
            return "Error: missing required parameter 'command'"
        if not path_raw:
            return "Error: missing required parameter 'path'"

        path = self._resolve(path_raw)

        if command == "view":
            return self._view(path, kwargs.get("view_range"))
        elif command == "create":
            file_text = kwargs.get("file_text")
            if file_text is None:
                return "Error: parameter `file_text` is required for command: create"
            return self._create(path, file_text)
        elif command == "str_replace":
            old_str = kwargs.get("old_str")
            if old_str is None:
                return "Error: parameter `old_str` is required for command: str_replace"
            new_str = kwargs.get("new_str")
            return self._str_replace(path, old_str, new_str)
        elif command == "insert":
            insert_line = kwargs.get("insert_line")
            if insert_line is None:
                return "Error: parameter `insert_line` is required for command: insert"
            new_str = kwargs.get("new_str")
            if new_str is None:
                return "Error: parameter `new_str` is required for command: insert"
            return self._insert(path, insert_line, new_str)
        elif command == "undo_edit":
            return self._undo_edit(path)
        else:
            return (
                f"Error: unrecognized command '{command}'. "
                f"Allowed commands: view, create, str_replace, insert, undo_edit"
            )

    # ------------------------------------------------------------------
    # view
    # ------------------------------------------------------------------

    def _view(self, path: str, view_range: list[int] | None) -> str:
        if self._io:
            # Docker mode: try to read as file first
            try:
                content = self._io.read_file(path)
            except FileNotFoundError:
                # Could be a directory — check via bash
                check = self._io.run_bash(f"test -d {path} && echo DIR || test -e {path} && echo FILE || echo MISSING")
                check = check.strip()
                if check == "DIR":
                    if view_range:
                        return "Error: the `view_range` parameter is not allowed when `path` points to a directory."
                    # List directory contents via bash (matching local _view_directory style)
                    listing = self._io.run_bash(
                        f"find {path} -maxdepth 2 -not -path '*/\\.*' | sort"
                    )
                    return (
                        f"Here's the files and directories up to 2 levels deep in {path}, "
                        f"excluding hidden items:\n{listing}"
                    )
                return f"Error: the path {path} does not exist. Please provide a valid path."
            return self._view_file_content(path, content, view_range)
        else:
            if not os.path.exists(path):
                return f"Error: the path {path} does not exist. Please provide a valid path."

            if os.path.isdir(path):
                if view_range:
                    return "Error: the `view_range` parameter is not allowed when `path` points to a directory."
                return self._view_directory(path)

            return self._view_file(path, view_range)

    def _view_directory(self, path: str) -> str:
        """List non-hidden files up to 2 levels deep."""
        entries: list[str] = []
        base = Path(path)
        for root, dirs, files in os.walk(base):
            # Calculate depth relative to base
            rel = Path(root).relative_to(base)
            depth = len(rel.parts)
            if depth > 2:
                continue
            # Filter hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(dirs + files):
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                entries.append(full)
        # Also add the base itself
        listing = "\n".join(sorted(entries))
        return (
            f"Here's the files and directories up to 2 levels deep in {path}, "
            f"excluding hidden items:\n{path}\n{listing}\n"
        )

    def _view_file(self, path: str, view_range: list[int] | None) -> str:
        try:
            if self._io:
                content = self._io.read_file(path)
            else:
                content = self._read_file(path)
        except Exception as e:
            return f"Error reading file: {e}"

        return self._view_file_content(path, content, view_range)

    def _view_file_content(self, path: str, content: str, view_range: list[int] | None) -> str:
        """Render file content with optional line range."""
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

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def _create(self, path: str, file_text: str) -> str:
        if self._io:
            # Check existence via IO
            try:
                self._io.read_file(path)
                return f"Error: file already exists at: {path}. Cannot overwrite files using command `create`."
            except FileNotFoundError:
                pass
        else:
            if os.path.exists(path):
                return f"Error: file already exists at: {path}. Cannot overwrite files using command `create`."

            parent = os.path.dirname(path)
            if parent and not os.path.exists(parent):
                return f"Error: the parent directory {parent} does not exist. Please create it first."

        try:
            if self._io:
                self._io.write_file(path, file_text)
            else:
                with open(path, "w") as f:
                    f.write(file_text)
        except Exception as e:
            return f"Error writing file: {e}"

        # Store initial content in undo history
        self._undo_history.setdefault(path, []).append(file_text)
        return f"File created successfully at: {path}"

    # ------------------------------------------------------------------
    # str_replace
    # ------------------------------------------------------------------

    def _str_replace(self, path: str, old_str: str, new_str: str | None) -> str:
        try:
            if self._io:
                content = self._io.read_file(path)
            else:
                content = self._read_file(path)
        except FileNotFoundError:
            return f"Error: file not found: {path}"
        except Exception as e:
            return f"Error reading file: {e}"

        if new_str is None:
            new_str = ""

        # Check occurrences
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

        # Build new content
        new_content = content.replace(old_str, new_str, 1)

        # Syntax check for Python files
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

        # Save current content for undo BEFORE writing
        self._undo_history.setdefault(path, []).append(content)

        try:
            if self._io:
                self._io.write_file(path, new_content)
            else:
                with open(path, "w") as f:
                    f.write(new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        # Build snippet around the edit
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

    # ------------------------------------------------------------------
    # insert
    # ------------------------------------------------------------------

    def _insert(self, path: str, insert_line: int, new_str: str) -> str:
        try:
            if self._io:
                content = self._io.read_file(path)
            else:
                content = self._read_file(path)
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

        # Save current content for undo
        self._undo_history.setdefault(path, []).append(content)

        try:
            if self._io:
                self._io.write_file(path, new_content)
            else:
                with open(path, "w") as f:
                    f.write(new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        # Build snippet around insertion
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

    # ------------------------------------------------------------------
    # undo_edit
    # ------------------------------------------------------------------

    def _undo_edit(self, path: str) -> str:
        history = self._undo_history.get(path, [])
        if not history:
            return f"Error: no edit history found for {path}."

        old_text = history.pop()
        try:
            if self._io:
                self._io.write_file(path, old_text)
            else:
                with open(path, "w") as f:
                    f.write(old_text)
        except Exception as e:
            return f"Error reverting file: {e}"

        return (
            f"Last edit to {path} undone successfully. "
            + _make_output(old_text, str(path))
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_file(path: str) -> str:
        """Read file content with encoding fallback."""
        encodings = [
            (None, None),
            ("utf-8", None),
            ("latin-1", None),
            ("utf-8", "replace"),
        ]
        exception = None
        for encoding, errors in encodings:
            try:
                with open(path, "r", encoding=encoding, errors=errors) as f:
                    return f.read()
            except UnicodeDecodeError as e:
                exception = e
        raise RuntimeError(f"UnicodeDecodeError while trying to read {path}: {exception}")
