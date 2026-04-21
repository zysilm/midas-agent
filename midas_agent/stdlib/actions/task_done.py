"""TaskDone action — signal task completion."""
from __future__ import annotations

from midas_agent.stdlib.action import Action

DONE_SENTINEL = "<<TASK_DONE_CONFIRMED>>"


class TaskDoneAction(Action):
    """Signals task/step completion."""

    def __init__(self, step_description: str | None = None) -> None:
        self._step_description = step_description

    def set_step(self, description: str) -> None:
        """Update the current step description (called by DAGExecutor on phase transition)."""
        self._step_description = description

    @property
    def name(self) -> str:
        return "task_done"

    @property
    def description(self) -> str:
        if self._step_description:
            return (
                f"Call this when you have completed the current step: "
                f"{self._step_description}"
            )
        return (
            "Signals that the current task is complete and submits your changes "
            "for evaluation. Make sure you have edited source files and verified "
            "your fix before calling this."
        )

    @property
    def parameters(self) -> dict:
        return {}

    def execute(self, **kwargs) -> str:
        return DONE_SENTINEL + " Task completed."
