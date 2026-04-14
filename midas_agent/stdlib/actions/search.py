"""Search actions — code search and file find."""
from midas_agent.stdlib.action import Action


class SearchCodeAction(Action):
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "search_code"

    @property
    def description(self) -> str:
        return (
            "Searches file contents using regular expressions. Backed by ripgrep. "
            "Returns file paths with matching lines, sorted by modification time.\n\n"
            "Usage:\n"
            "* Supports full regex syntax (e.g. `\"log.*Error\"`, `\"function\\s+\\w+\"`).\n"
            "* Filter by file type with the `include` parameter (e.g. `\"*.py\"`).\n"
            "* Returns file paths with at least one match, not full file contents. "
            "Use `read_file` to inspect matches in detail.\n"
            "* You can call multiple tools in a single response. It is always "
            "better to speculatively perform multiple searches in parallel."
        )

    @property
    def parameters(self) -> dict:
        return {
            "pattern": {"type": "string", "required": True},
            "path": {"type": "string", "required": False},
            "include": {"type": "string", "required": False},
        }

    def execute(self, **kwargs) -> str:
        pattern = kwargs["pattern"]
        return f"Search results for: {pattern}"


class FindFilesAction(Action):
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Finds files by name using glob patterns. Returns matching file paths "
            "sorted by modification time.\n\n"
            "Usage:\n"
            "* Supports glob patterns like `\"**/*.py\"`, `\"src/**/*.ts\"`.\n"
            "* Use this when you need to locate files by name pattern, not by content. "
            "For content search, use `search_code`.\n"
            "* You can call multiple tools in a single response. It is always "
            "better to speculatively perform multiple searches in parallel."
        )

    @property
    def parameters(self) -> dict:
        return {
            "pattern": {"type": "string", "required": True},
            "path": {"type": "string", "required": False},
        }

    def execute(self, **kwargs) -> str:
        pattern = kwargs["pattern"]
        return f"Found files matching: {pattern}"
