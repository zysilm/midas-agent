"""Bash action — shell command execution."""
import subprocess

from midas_agent.stdlib.action import Action


class BashAction(Action):
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Runs a bash command and returns its output."

    @property
    def parameters(self) -> dict:
        return {
            "command": {"type": "string", "required": True, "description": "The bash command to execute."},
        }

    def execute(self, **kwargs) -> str:
        command = kwargs["command"]
        timeout = kwargs.get("timeout", 120)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.cwd or None,
            )
            output = result.stdout
            if result.returncode != 0 and result.stderr:
                output += result.stderr
            return output
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing command: {e}"
