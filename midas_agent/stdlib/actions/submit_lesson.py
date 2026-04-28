"""SubmitLesson action — structured lesson extraction from failure analysis."""
from __future__ import annotations

from dataclasses import dataclass

from midas_agent.stdlib.action import Action

LESSON_SENTINEL = "<<LESSON_SUBMITTED>>"


@dataclass
class SubmittedLesson:
    step_id: str
    mistake: str
    lesson: str


class SubmitLessonAction(Action):
    """Tool for the failure analyzer to submit a structured lesson."""

    def __init__(self) -> None:
        self.submitted: SubmittedLesson | None = None

    @property
    def name(self) -> str:
        return "submit_lesson"

    @property
    def description(self) -> str:
        return (
            "Submit your failure analysis as a structured lesson. "
            "Call this exactly once with the step that failed, "
            "what the agent did wrong, and the abstract lesson."
        )

    @property
    def parameters(self) -> dict:
        return {
            "step_id": {
                "type": "string",
                "required": True,
                "description": "Which step failed (e.g. 'fix', 'localize', 'reproduce')",
            },
            "mistake": {
                "type": "string",
                "required": True,
                "description": "What specifically the agent did wrong",
            },
            "lesson": {
                "type": "string",
                "required": True,
                "description": (
                    "One-sentence abstract lesson for future runs. "
                    "No file or function names — must generalize to other issues."
                ),
            },
        }

    def execute(self, **kwargs) -> str:
        step_id = kwargs.get("step_id", "")
        mistake = kwargs.get("mistake", "")
        lesson = kwargs.get("lesson", "")

        if not step_id or not lesson:
            return "Error: step_id and lesson are required."

        self.submitted = SubmittedLesson(
            step_id=step_id.strip().lower(),
            mistake=mistake.strip(),
            lesson=lesson.strip(),
        )
        return LESSON_SENTINEL + " Lesson recorded."
