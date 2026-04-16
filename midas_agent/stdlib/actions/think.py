"""Think action — structured reasoning without side effects."""
from midas_agent.stdlib.action import Action


class ThinkAction(Action):
    @property
    def name(self) -> str:
        return "think"

    @property
    def description(self) -> str:
        return (
            "Use this tool to reason through a problem before taking action. "
            "It does not execute anything or modify any files — it simply logs "
            "your thought process and returns it for reference.\n\n"
            "Use it when:\n"
            "1. Analyzing a bug — brainstorm what the root cause is and what "
            "the simplest fix would be before editing code.\n"
            "2. After test failures — reason about why a test failed and what "
            "to try differently.\n"
            "3. Planning a multi-step change — outline your approach before "
            "executing it.\n"
            "4. When stuck — step back and reconsider your assumptions.\n\n"
            "Good thinking leads to fewer wasted tool calls and better fixes."
        )

    @property
    def parameters(self) -> dict:
        return {
            "thought": {
                "type": "string",
                "description": "Your reasoning, analysis, or plan.",
                "required": True,
            },
        }

    def execute(self, **kwargs) -> str:
        thought = kwargs.get("thought", "")
        if not thought or not thought.strip():
            return "Error: 'thought' must be a non-empty string."
        return "Your thought has been logged."
