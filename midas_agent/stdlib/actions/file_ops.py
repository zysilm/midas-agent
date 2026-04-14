"""File operation actions — read, edit, write."""
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
        raise NotImplementedError


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
        raise NotImplementedError


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
        raise NotImplementedError
