"""Bash action — shell command execution."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from midas_agent.stdlib.action import Action

if TYPE_CHECKING:
    from midas_agent.runtime.io_backend import IOBackend


class BashAction(Action):
    def __init__(self, cwd: str | None = None, io: IOBackend | None = None) -> None:
        self.cwd = cwd
        self._io = io

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
            if self._io is not None:
                return self._io.run_bash(command, cwd=self.cwd, timeout=timeout)
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
