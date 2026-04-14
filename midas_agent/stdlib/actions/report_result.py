"""ReportResult action — hired agent reports back (Graph Emergence only)."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action


class ReportResultAction(Action):
    def __init__(self, report: Callable) -> None:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return "report_result"

    @property
    def description(self) -> str:
        return "Report task result to the hiring agent."

    @property
    def parameters(self) -> dict:
        return {
            "result": {"type": "string", "required": True},
            "status": {"type": "string", "required": False, "default": "success"},
        }

    def execute(self, **kwargs) -> str:
        raise NotImplementedError
