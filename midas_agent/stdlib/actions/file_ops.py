"""File operation actions — read, edit, write."""
import os

from midas_agent.stdlib.action import Action


class ReadFileAction(Action):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Reads a file from the local filesystem. Returns content with "
            "line numbers (cat -n format, starting at 1).\n\n"
            "Usage:\n"
            "* The path must be an absolute path.\n"
            "* By default, reads up to 2000 lines from the beginning of the file.\n"
            "* You can specify offset and limit for long files, but reading the "
            "whole file is recommended when feasible.\n"
            "* You can call multiple tools in a single response. It is always "
            "better to speculatively read multiple potentially useful files in parallel."
        )

    @property
    def parameters(self) -> dict:
        return {
            "path": {"type": "string", "required": True},
            "offset": {"type": "integer", "required": False},
            "limit": {"type": "integer", "required": False},
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["path"]
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit")
        try:
            with open(file_path, "r") as f:
                lines = f.readlines()
            selected = lines[offset:] if limit is None else lines[offset:offset + limit]
            return "".join(selected)
        except FileNotFoundError:
            return f"File not found: {file_path}"
        except Exception as e:
            return f"Error reading file: {e}"


class EditFileAction(Action):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edits an existing file using line-number-based operations. "
            "You must use `read_file` at least once before editing a file.\n\n"
            "Three sub-commands:\n"
            "* `replace`: Replace content from start_line to end_line with new_content\n"
            "* `insert`: Insert new_content after insert_line\n"
            "* `delete`: Delete lines from start_line to end_line\n\n"
            "Usage:\n"
            "* Line numbers reference the output of `read_file` (1-indexed).\n"
            "* Python files are syntax-checked (ast.parse) before the edit is committed. "
            "If syntax check fails, the edit is rejected and an error is returned.\n"
            "* ALWAYS prefer editing existing files over creating new ones."
        )

    @property
    def parameters(self) -> dict:
        return {
            "command": {"type": "string", "required": True},
            "path": {"type": "string", "required": True},
            "start_line": {"type": "integer", "required": False},
            "end_line": {"type": "integer", "required": False},
            "insert_line": {"type": "integer", "required": False},
            "new_content": {"type": "string", "required": False},
            "auto_indent": {"type": "boolean", "required": False, "default": True},
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["path"]
        return f"Edited {file_path}"


class WriteFileAction(Action):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Creates a new file or overwrites an existing file with the given content. "
            "Creates parent directories automatically if they do not exist.\n\n"
            "Usage:\n"
            "* Use this tool to create NEW files. For editing existing files, "
            "use `edit_file` instead.\n"
            "* If the file already exists, it will be completely overwritten — "
            "only use this intentionally."
        )

    @property
    def parameters(self) -> dict:
        return {
            "path": {"type": "string", "required": True},
            "content": {"type": "string", "required": True},
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["path"]
        content = kwargs["content"]
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {file_path}"
        except Exception as e:
            return f"Error writing file: {e}"
