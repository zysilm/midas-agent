"""Bash action — shell command execution."""
from midas_agent.stdlib.action import Action


class BashAction(Action):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Execute a shell command."

    @property
    def parameters(self) -> dict:
        return {
            "command": {"type": "string", "required": True},
            "timeout": {"type": "integer", "required": False, "default": 120},
        }

    def execute(self, **kwargs) -> str:
        raise NotImplementedError
