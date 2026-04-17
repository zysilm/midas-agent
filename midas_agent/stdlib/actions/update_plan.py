"""UpdatePlan action — structured task planning with progress tracking."""
from __future__ import annotations

import json

from midas_agent.stdlib.action import Action


class UpdatePlanAction(Action):
    """Maintains a structured plan that the agent updates as it works.

    The plan is returned as formatted text so it appears in the
    conversation history, giving the LLM a persistent view of progress.
    """

    def __init__(self) -> None:
        self._current_plan: list[dict] | None = None

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "Create or update the task plan. Provide a list of steps with "
            "their status. Use this to decompose the task upfront, then "
            "update step statuses as you make progress. At most one step "
            "should be in_progress at a time."
        )

    @property
    def parameters(self) -> dict:
        return {
            "explanation": {
                "type": "string",
                "required": False,
                "description": "Brief rationale for this plan update.",
            },
            "plan": {
                "type": "array",
                "required": True,
                "description": "List of plan steps. Each has 'step' (description) and 'status' (pending, in_progress, or completed).",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                },
            },
        }

    def execute(self, **kwargs) -> str:
        plan = kwargs.get("plan")
        explanation = kwargs.get("explanation", "")

        if not plan or not isinstance(plan, list):
            return "Error: 'plan' must be a non-empty list of {step, status} items."

        # Parse plan items from dicts or JSON strings
        items: list[dict] = []
        for entry in plan:
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except (json.JSONDecodeError, TypeError):
                    return f"Error: could not parse plan item: {entry!r}"
            step = entry.get("step", "")
            status = entry.get("status", "pending")
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            items.append({"step": step, "status": status})

        # Validate: at most one in_progress
        in_progress = [i for i in items if i["status"] == "in_progress"]
        if len(in_progress) > 1:
            return "Error: at most one step can be in_progress at a time."

        self._current_plan = items

        # Format as readable text
        status_icons = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
        }
        lines: list[str] = []
        if explanation:
            lines.append(explanation)
            lines.append("")
        completed = sum(1 for i in items if i["status"] == "completed")
        lines.append(f"Plan ({completed}/{len(items)} completed):")
        for i, item in enumerate(items, 1):
            icon = status_icons.get(item["status"], "[ ]")
            lines.append(f"  {i}. {icon} {item['step']}")

        return "\n".join(lines)
