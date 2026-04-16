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
        return (
            "Reads a file from the local filesystem. You can access any file "
            "directly by using this tool.\n\n"
            f"{cwd_note}"
            "Usage:\n"
            " - Use relative paths from the working directory (e.g. "
            "`./src/module.py`), or absolute paths. Relative paths are "
            "resolved against the working directory.\n"
            " - By default, reads up to 2000 lines starting from the "
            "beginning of the file.\n"
            " - For large files, use `offset` and `limit` to read specific "
            "portions. When you already know which part of the file you need, "
            "only read that part — this saves tokens.\n"
            " - Results are returned with line numbers (1-indexed). Use these "
            "line numbers when calling `edit_file`.\n"
            " - You can call multiple tools in a single response. When multiple "
            "files might be relevant, read them all in parallel rather than "
            "sequentially — this is faster and avoids wasting iterations.\n"
            " - If the file does not exist, an error message is returned with "
            "the resolved path. Check the path and try again.\n\n"
            "IMPORTANT: You must read a file with this tool before editing it "
            "with `edit_file`. The edit tool will reference line numbers from "
            "this tool's output."
        )

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
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Performs exact string replacement in a file.\n\n"
            "Usage:\n"
            " - Specify `old_string` (the exact text to find) and `new_string` "
            "(the replacement). The tool replaces the first — and only — "
            "occurrence of `old_string` with `new_string`.\n"
            " - `old_string` must be unique in the file. If it matches "
            "multiple locations, the edit is rejected. Provide more "
            "surrounding context to make the match unique.\n"
            " - For `.py` files the result is syntax-checked with `ast.parse` "
            "before being committed. If the syntax check fails, the edit is "
            "rejected and the file is left unchanged.\n"
            " - Prefer this tool for editing existing files. It only changes "
            "the text you specify and preserves everything else.\n"
            " - To delete text, set `new_string` to an empty string.\n"
            " - Multiple edits on the same file do not require re-reading — "
            "content matching is immune to line-number drift."
        )

    @property
    def parameters(self) -> dict:
        return {
            "path": {"type": "string", "required": True},
            "old_string": {"type": "string", "required": True},
            "new_string": {"type": "string", "required": True},
        }

    def _resolve(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        if self.cwd:
            return os.path.join(self.cwd, path)
        return path

    def execute(self, **kwargs) -> str:
        try:
            file_path = self._resolve(kwargs["path"])
        except KeyError:
            return "Error: missing required parameter 'path'"

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
                return f"Syntax error: {e}. Edit rejected; file unchanged."

        # Write back
        try:
            with open(file_path, "w") as f:
                f.write(new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        return f"Edited {file_path}"


class WriteFileAction(Action):
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Writes a file to the local filesystem. Creates parent directories "
            "automatically if they do not exist.\n\n"
            "Usage:\n"
            " - This tool will overwrite the existing file if there is one at "
            "the provided path.\n"
            " - Prefer `edit_file` for modifying existing files — it only "
            "changes the lines you specify and preserves the rest. Only use "
            "`write_file` to create genuinely new files (e.g. test scripts, "
            "reproduction scripts).\n"
            " - NEVER use `write_file` to rewrite an entire source file when "
            "you only need to change a few lines. Use `edit_file` instead — "
            "the resulting diff will be cleaner and the patch more reviewable.\n\n"
            "IMPORTANT: New files created with `write_file` are useful for "
            "testing and reproduction, but they are NOT the fix. To fix a "
            "bug, you must edit the existing source files with `edit_file`. "
            "Your score is based on whether the repository's failing tests "
            "pass after your changes, not on new files you create."
        )

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
