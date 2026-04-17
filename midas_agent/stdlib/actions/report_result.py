"""ReportResult action — hired agent reports back (Graph Emergence only)."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action


class ReportResultAction(Action):
    def __init__(self, report: Callable) -> None:
        self._report = report

    @property
    def name(self) -> str:
        return "report_result"

    @property
    def description(self) -> str:
        return (
            "Returns your results to the agent that hired you. This is the "
            "only way to communicate results back to your employer.\n\n"
            "Usage:\n"
            " - Call this exactly once when your assigned sub-task is "
            "complete.\n"
            " - Write a clear, actionable summary: what you found, what "
            "files are relevant, what the root cause is, and what fix you "
            "recommend. Your employer will use this to decide next steps.\n"
            " - Include specific file paths and line numbers when "
            "referencing code — your employer does not share your context "
            "window.\n"
            " - After calling this, your session ends. No further actions "
            "will be executed.\n\n"
            "IMPORTANT: Do not just say 'done' or 'task complete'. Provide "
            "enough detail that your employer can act on your findings "
            "without having to redo your work."
        )

    @property
    def parameters(self) -> dict:
        return {
            "result": {"type": "string", "required": True, "description": "Clear, actionable summary of your findings. Include file paths and line numbers."},
        }

    def execute(self, **kwargs) -> str:
        result = kwargs["result"]
        self._report(result)
        return f"Result reported."
