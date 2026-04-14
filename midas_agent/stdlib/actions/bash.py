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
        return (
            "Executes a bash command with optional timeout. Working directory "
            "persists between calls; shell state (environment variables, shell "
            "functions, virtual environments) does not — each call starts from "
            "a clean shell initialized from user profile.\n\n"
            "Usage:\n"
            "* Write a clear, concise description of what the command does in 5-10 words.\n"
            "* When issuing multiple commands, use `;` or `&&` to separate them. "
            "Do NOT use newlines to separate commands.\n"
            "* Always quote file paths that contain spaces with double quotes.\n\n"
            "IMPORTANT:\n"
            "* Do NOT use `cat`, `head`, `tail` to read files — use `read_file` instead.\n"
            "* Do NOT use `grep` or `find` to search — use `search_code` or `find_files` instead.\n"
            "* Do NOT use `sed` or `awk` for file edits — use `edit_file` instead.\n"
            "* Interactive commands (e.g. `python`, `vim`, `nano`) are NOT supported."
        )

    @property
    def parameters(self) -> dict:
        return {
            "command": {"type": "string", "required": True},
            "timeout": {"type": "integer", "required": False, "default": 120},
            "description": {"type": "string", "required": False},
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
