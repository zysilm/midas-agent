"""Conversation compaction — build prompts and histories for LLM-based compression."""
from __future__ import annotations

from midas_agent.context.truncation import truncate_output

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMPACTION_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"""

# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def should_compact(total_tokens: int, context_window: int, ratio: float = 0.9) -> bool:
    """Return ``True`` when *total_tokens* reaches *ratio* of the context window."""
    if context_window <= 0:
        return False
    return total_tokens >= int(context_window * ratio)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_compaction_prompt(messages: list[dict]) -> list[dict]:
    """Return a message list suitable for asking an LLM to produce a compaction summary.

    The returned list contains all original *messages* followed by a final user
    message with the ``COMPACTION_PROMPT``.
    """
    return list(messages) + [{"role": "user", "content": COMPACTION_PROMPT}]


# ---------------------------------------------------------------------------
# Post-compaction history
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token per 4 characters."""
    return len(text) // 4


def build_compacted_history(
    old_messages: list[dict],
    summary: str,
    max_user_message_tokens: int = 20000,
) -> list[dict]:
    """Build a new, smaller conversation history after compaction.

    Algorithm:
    1. Extract only ``role="user"`` messages from *old_messages*.
    2. Reserve the first user message (the issue description) — it is always
       kept in full and placed first in the result.
    3. Walk the remaining user messages from newest to oldest, accumulating
       until *max_user_message_tokens* is exhausted.  If a message does not
       fully fit, it is truncated (middle-elision) to fill the remaining
       budget, then iteration stops.
    4. Reverse the collected messages back to chronological order.
    5. Build result: ``[first_user_message] + [recent messages] + [summary]``.
    """
    user_messages = [m for m in old_messages if m.get("role") == "user"]

    # --- Reserve the first user message (the issue) -----------------------
    first_user_msg: dict | None = None
    remaining_user_messages: list[dict] = user_messages
    if user_messages:
        first_user_msg = user_messages[0]
        remaining_user_messages = user_messages[1:]

    budget_chars = max_user_message_tokens * 4  # inverse of token estimate
    collected: list[dict] = []
    used_chars = 0

    # Iterate newest-first over remaining (non-issue) user messages
    for msg in reversed(remaining_user_messages):
        content = msg["content"]
        msg_chars = len(content)

        remaining = budget_chars - used_chars
        if remaining <= 0:
            break

        if msg_chars <= remaining:
            collected.append({"role": "user", "content": content})
            used_chars += msg_chars
        else:
            # Truncate this message to fit and stop
            truncated = truncate_output(content, max_chars=remaining)
            collected.append({"role": "user", "content": truncated})
            break

    # Reverse to chronological order
    collected.reverse()

    # Build result: issue first, then recent messages, then summary
    result: list[dict] = []
    if first_user_msg is not None:
        result.append({"role": "user", "content": first_user_msg["content"]})
    result.extend(collected)

    # Append compaction summary
    result.append({"role": "user", "content": SUMMARY_PREFIX + "\n" + summary})

    return result
