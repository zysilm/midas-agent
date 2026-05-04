from __future__ import annotations

from typing import Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import Action


from llm_agent_toolkit.types import ActionEvent  # noqa: F401


class TUI:
    def __init__(
        self,
        call_llm: Callable[[LLMRequest], LLMResponse],
        actions: list[Action],
        system_prompt: str,
        on_action: Callable[[ActionEvent], None] | None = None,
        action_log: "IO | None" = None,
    ) -> None:
        self._call_llm = call_llm
        self._actions = actions
        self._system_prompt = system_prompt
        self._on_action = on_action
        self._action_log = action_log

    def run(self) -> None:
        """REPL loop: read input, run agent, display output."""
        from midas_agent.stdlib.react_agent import ReactAgent

        print("Midas Agent")  # welcome message

        while True:
            try:
                user_input = input("> ")
            except (EOFError, KeyboardInterrupt):
                return

            stripped = user_input.strip()
            if stripped in ("/quit", "/exit"):
                return
            if not stripped:
                continue

            agent = ReactAgent(
                system_prompt=self._system_prompt,
                actions=self._actions,
                call_llm=self._call_llm,
                on_action=self._on_action,
                action_log=self._action_log,
            )
            result = agent.run(context=stripped)
            if result.output:
                print(result.output)
