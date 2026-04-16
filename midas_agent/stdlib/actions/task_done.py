"""TaskDone action — signal task completion."""
from midas_agent.stdlib.action import Action


class TaskDoneAction(Action):
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
        return kwargs.get("summary", "Task completed.")
