"""TaskDone action — signal task completion."""
from __future__ import annotations

from midas_agent.stdlib.action import Action

DONE_SENTINEL = "<<TASK_DONE_CONFIRMED>>"


class TaskDoneAction(Action):
    """Signals task completion and submits the patch."""

    @property
    def name(self) -> str:
        return "task_done"

    @property
    def description(self) -> str:
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
