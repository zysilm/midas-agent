"""TaskDone action — two-phase submission with review gate.

When constructed with a `get_diff` callback, the first call returns the
current diff and a review checklist.  The agent must call task_done a
second time to actually confirm submission.

Without `get_diff` (the default), task_done confirms immediately — this
keeps all existing tests and inference paths unchanged.
"""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action

REVIEW_MESSAGE = """\
Before submitting, please review your changes carefully.

1. If you made changes after your last test run, re-run your reproduction script to verify.
2. Remove any reproduction/debug scripts you created (they will pollute the patch).
3. If you modified any test files, revert them with `bash(command='git checkout -- <path>')`.
4. Confirm your fix addresses the root cause, not just the symptoms.

Here is a diff of all your changes:

<diff>
{diff}
</diff>

If everything looks correct, call `task_done` again to confirm submission.\
"""

# Sentinel prefix that the agent loop checks to distinguish
# a real submission from the review prompt.
DONE_SENTINEL = "<<TASK_DONE_CONFIRMED>>"


class TaskDoneAction(Action):
    def __init__(self, get_diff: Callable[[], str] | None = None) -> None:
        self._get_diff = get_diff
        self._review_pending = False

    @property
    def name(self) -> str:
        return "task_done"

    @property
    def description(self) -> str:
        if self._get_diff is not None:
            return (
                "Signals that the current task is complete and submits your changes "
                "for evaluation. The first call triggers a review of your diff — "
                "check it carefully and call task_done again to confirm."
            )
        return (
            "Signals that the current task is complete and submits your changes "
            "for evaluation. Make sure you have edited source files and verified "
            "your fix before calling this."
        )

    @property
    def parameters(self) -> dict:
        return {}

    def execute(self, **kwargs) -> str:
        # No get_diff → immediate confirmation (backward-compatible path)
        if self._get_diff is None:
            return DONE_SENTINEL + " " + kwargs.get("summary", "Task completed.")

        # Review gate: first call returns diff + checklist
        if not self._review_pending:
            self._review_pending = True
            try:
                diff = self._get_diff()
            except Exception:
                diff = "(could not generate diff)"
            if not diff or not diff.strip():
                diff = "(no changes detected)"
            return REVIEW_MESSAGE.format(diff=diff)

        # Second call — actual submission
        self._review_pending = False
        return DONE_SENTINEL + " Task completed."
