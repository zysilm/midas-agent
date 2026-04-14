"""TaskDone action — signal task completion."""
from midas_agent.stdlib.action import Action


class TaskDoneAction(Action):
    @property
    def name(self) -> str:
        return "task_done"

    @property
    def description(self) -> str:
        return "Mark current task as completed."

    @property
    def parameters(self) -> dict:
        return {"summary": {"type": "string", "required": True}}

    def execute(self, **kwargs) -> str:
        raise NotImplementedError
