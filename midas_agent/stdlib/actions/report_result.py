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
            "Returns results to the agent that hired you. You must call this "
            "exactly once when your assigned task is complete.\n\n"
            "Usage:\n"
            "* This is the only way to communicate results back to your employer.\n"
            "* Write a clear, concise summary of what you did and what the outcome was.\n"
            "* After calling this, your session for this task ends."
        )

    @property
    def parameters(self) -> dict:
        return {
            "result": {"type": "string", "required": True},
        }

    def execute(self, **kwargs) -> str:
        result = kwargs["result"]
        self._report(result)
        return f"Result reported."
