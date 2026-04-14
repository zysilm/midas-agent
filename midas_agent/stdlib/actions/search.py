"""Search actions — code search and file find."""
from midas_agent.stdlib.action import Action


class SearchCodeAction(Action):
    @property
    def name(self) -> str:
        return "search_code"

    @property
    def description(self) -> str:
        return "Search code with regex pattern."

    @property
    def parameters(self) -> dict:
        return {
            "pattern": {"type": "string", "required": True},
            "path": {"type": "string", "required": False},
            "file_glob": {"type": "string", "required": False},
            "max_results": {"type": "integer", "required": False, "default": 50},
        }

    def execute(self, **kwargs) -> str:
        raise NotImplementedError


class FindFilesAction(Action):
    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return "Find files by glob pattern."

    @property
    def parameters(self) -> dict:
        return {
            "pattern": {"type": "string", "required": True},
            "path": {"type": "string", "required": False},
            "max_results": {"type": "integer", "required": False, "default": 100},
        }

    def execute(self, **kwargs) -> str:
        raise NotImplementedError
