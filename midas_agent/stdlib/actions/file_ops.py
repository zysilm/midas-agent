"""File operation actions — read, edit, write."""
import ast
import os

from midas_agent.stdlib.action import Action

DEFAULT_READ_LIMIT = 200


class ReadFileAction(Action):
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        cwd_note = f"The current working directory is: {self.cwd}\n" if self.cwd else ""
        return f"Reads a file and returns contents with line numbers.\n{cwd_note}"

    @property
    def parameters(self) -> dict:
        return {
            "path": {"type": "string", "required": True},
            "offset": {"type": "integer", "required": False},
            "limit": {"type": "integer", "required": False},
        }

    def _resolve(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        if self.cwd:
            return os.path.join(self.cwd, path)
        return path

    def execute(self, **kwargs) -> str:
        file_path = self._resolve(kwargs["path"])
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit")
        # Apply default limit when caller does not specify one
        effective_limit = limit if limit is not None else DEFAULT_READ_LIMIT
        try:
            with open(file_path, "r") as f:
                lines = f.readlines()
            total_lines = len(lines)
            end_index = min(offset + effective_limit, total_lines)
            selected = lines[offset:end_index]

            # Format with line numbers (1-indexed, accounting for offset)
            numbered: list[str] = []
            for i, line in enumerate(selected):
                line_num = offset + i + 1  # 1-indexed
                # Strip trailing newline for consistent formatting
                content = line.rstrip("\n")
                numbered.append(f"{line_num:>6}\t{content}")

            result = "\n".join(numbered)

            # Append truncation indicator if file has more lines than shown
            if end_index < total_lines:
                start_num = offset + 1
                end_num = end_index
                result += (
                    f"\n[File has {total_lines} lines total. "
                    f"Showing lines {start_num}-{end_num}. "
                    f"Use offset and limit to read more.]"
                )

            return result
        except FileNotFoundError:
            return f"File not found: {file_path}"
        except Exception as e:
            return f"Error reading file: {e}"


class EditFileAction(Action):
    _undo_history: dict[str, str] = {}

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Performs string replacement in a file. Shows the edited region "
            "with line numbers after a successful edit.\n\n"
            "Notes:\n"
            "* The `old_string` must match EXACTLY one occurrence in the file. "
            "Be mindful of whitespace!\n"
            "* If `old_string` is not unique, the replacement will not be performed.\n"
            "* Set `undo=True` with a `path` to revert the last edit made to that file."
        )

    @property
    def parameters(self) -> dict:
        return {
            "path": {"type": "string", "required": True},
            "old_string": {"type": "string", "required": True},
            "new_string": {"type": "string", "required": True},
            "undo": {"type": "boolean", "required": False, "description": "If True, revert the last edit to the file at path."},
        }

    def _resolve(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        if self.cwd:
            return os.path.join(self.cwd, path)
        return path

    def _snippet(self, content: str, target: str, path: str, context_lines: int = 4) -> str:
        """Return a cat -n style snippet around *target* in *content*."""
        lines = content.splitlines(keepends=True)
        # Find the first line that contains part of target
        target_first_line = target.splitlines()[0] if target else ""
        center = 0
        for i, line in enumerate(lines):
            if target_first_line and target_first_line in line:
                center = i
                break
        start = max(0, center - context_lines)
        end = min(len(lines), center + len(target.splitlines()) + context_lines)
        snippet_lines: list[str] = []
        for i in range(start, end):
            num = i + 1  # 1-indexed
            text = lines[i].rstrip("\n")
            snippet_lines.append(f"    {num}\t{text}")
        header = f"The file {path} has been edited. Here's the result of running `cat -n` on a snippet:"
        return header + "\n" + "\n".join(snippet_lines)

    def execute(self, **kwargs) -> str:
        try:
            file_path = self._resolve(kwargs["path"])
        except KeyError:
            return "Error: missing required parameter 'path'"

        # Handle undo
        if kwargs.get("undo"):
            if file_path in EditFileAction._undo_history:
                prev = EditFileAction._undo_history.pop(file_path)
                try:
                    with open(file_path, "w") as f:
                        f.write(prev)
                    return f"Reverted {file_path} to previous content."
                except Exception as e:
                    return f"Error reverting file: {e}"
            return f"No undo history for {file_path}"

        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        if old_string is None or new_string is None:
            return "Error: missing required parameter 'old_string' or 'new_string'"

        # Read existing file
        try:
            with open(file_path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return f"Error: file not found: {file_path}"
        except Exception as e:
            return f"Error reading file: {e}"

        # Check occurrences of old_string
        count = content.count(old_string)
        if count == 0:
            return f"old_string not found in {file_path}"
        if count > 1:
            return (
                f"old_string is not unique in {file_path} "
                f"(found {count} occurrences). "
                f"Provide more surrounding context to make it unique."
            )

        # Exactly one occurrence — replace
        new_content = content.replace(old_string, new_string, 1)

        # Syntax check for Python files
        if file_path.endswith(".py"):
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

        # Save current content for undo before writing
        EditFileAction._undo_history[file_path] = content

        # Write back
        try:
            with open(file_path, "w") as f:
                f.write(new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        return self._snippet(new_content, new_string, kwargs["path"])


class WriteFileAction(Action):
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Creates or overwrites a file with the given content."

    @property
    def parameters(self) -> dict:
        return {
            "path": {"type": "string", "required": True},
            "content": {"type": "string", "required": True},
        }

    def _resolve(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        if self.cwd:
            return os.path.join(self.cwd, path)
        return path

    def execute(self, **kwargs) -> str:
        file_path = self._resolve(kwargs["path"])
        content = kwargs["content"]
        try:
            dir_name = os.path.dirname(file_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(file_path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {file_path}"
        except Exception as e:
            return f"Error writing file: {e}"
