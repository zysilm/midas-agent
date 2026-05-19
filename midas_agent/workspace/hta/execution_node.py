"""ExecutionNode — the inner layer of HTA.

Between decision points the agent runs a vanilla ReAct loop. An ExecutionNode
is a thin wrapper that constructs a fresh, isolated ReactAgent per run, so each
call — including each competing hypothesis at a decision point — gets its own
conversation context (Direction A context isolation). Losing hypotheses' agents
simply go out of scope; their context never touches the main trace.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.stdlib.action import Action
from llm_agent_toolkit.stdlib.react_agent import ActionRecord, ReactAgent

logger = logging.getLogger(__name__)


@dataclass
class ExecutionOutcome:
    output: str
    action_history: list[ActionRecord] = field(default_factory=list)
    termination_reason: str = "done"
    iterations: int = 0
    stuck: bool = False
    error: str | None = None


class ExecutionNode:
    """Runs one isolated ReAct loop and reports whether it got stuck."""

    def __init__(
        self,
        system_prompt: str,
        actions: list[Action],
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse] | None = None,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        balance_provider: Callable[[], int] | None = None,
        max_iterations: int | None = None,
        action_log=None,
    ) -> None:
        self._system_prompt = system_prompt
        self._actions = actions
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._max_tool_output_chars = max_tool_output_chars
        self._max_context_tokens = max_context_tokens
        self._balance_provider = balance_provider
        self._max_iterations = max_iterations
        self._action_log = action_log

    def run(self, context: str) -> ExecutionOutcome:
        """Construct a fresh ReactAgent, run it, and wrap the result.

        Never raises — an internal failure is reported as an ExecutionOutcome
        with ``error`` set and ``termination_reason == "error"``.
        """
        agent = ReactAgent(
            system_prompt=self._system_prompt,
            actions=self._actions,
            call_llm=self._call_llm,
            max_iterations=self._max_iterations,
            balance_provider=self._balance_provider,
            max_tool_output_chars=self._max_tool_output_chars,
            max_context_tokens=self._max_context_tokens,
            system_llm=self._system_llm,
            action_log=self._action_log,
        )
        try:
            result = agent.run(context=context)
        except Exception as e:  # noqa: BLE001 — engine must survive node failure
            logger.warning("ExecutionNode failed: %s", e)
            return ExecutionOutcome(
                output="", termination_reason="error", error=str(e), stuck=True,
            )

        stuck = (
            ReactAgent._check_stuck(result.action_history) is not None
            or result.termination_reason in ("max_iterations", "no_action")
        )
        return ExecutionOutcome(
            output=result.output,
            action_history=result.action_history,
            termination_reason=result.termination_reason,
            iterations=result.iterations,
            stuck=stuck,
        )
