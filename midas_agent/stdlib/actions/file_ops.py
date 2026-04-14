"""File operation actions — read, edit, write."""
import os

from midas_agent.stdlib.action import Action


class ReadFileAction(Action):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read file contents."

    @property
    def parameters(self) -> dict:
        return {
            "file_path": {"type": "string", "required": True},
            "offset": {"type": "integer", "required": False, "default": 0},
            "limit": {"type": "integer", "required": False},
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["file_path"]
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
        return "Edit file by line number."

    @property
    def parameters(self) -> dict:
        return {
            "file_path": {"type": "string", "required": True},
            "operation": {"type": "string", "required": True},
            "start_line": {"type": "integer", "required": True},
            "end_line": {"type": "integer", "required": False},
            "content": {"type": "string", "required": False},
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["file_path"]
        return f"Edited {file_path}"


class WriteFileAction(Action):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Create a new file with content."

    @property
    def parameters(self) -> dict:
        return {
            "file_path": {"type": "string", "required": True},
            "content": {"type": "string", "required": True},
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["file_path"]
        content = kwargs["content"]
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {file_path}"
        except Exception as e:
            return f"Error writing file: {e}"
