"""TaskDone action — signal task completion."""
from midas_agent.stdlib.action import Action


class TaskDoneAction(Action):
    @property
    def name(self) -> str:
        return "task_done"

    @property
    def description(self) -> str:
        return (
            "Signals that the current task is complete and ready for evaluation. "
            "This is the final action in a workspace's execution.\n\n"
            "Usage:\n"
            "* Call this when you believe the issue has been resolved and all "
            "necessary changes have been made.\n"
            "* After calling this, no further actions will be executed.\n"
            "* If you run out of budget without calling this, the workspace is "
            "evaluated as-is (which may result in a low score)."
        )

    @property
    def parameters(self) -> dict:
        return {}

    def execute(self, **kwargs) -> str:
        return kwargs.get("summary", "Task completed.")
