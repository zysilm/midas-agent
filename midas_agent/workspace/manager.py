"""Workspace manager — lifecycle management for all workspaces."""
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from midas_agent.config import MidasConfig
from midas_agent.llm.types import LLMRequest, LLMResponse

if TYPE_CHECKING:
    from midas_agent.workspace.base import Workspace


class WorkspaceManager:
    def __init__(
        self,
        config: MidasConfig,
        call_llm_factory: Callable[[str], Callable],
        system_llm_callback: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        raise NotImplementedError

    @property
    def workspaces(self) -> dict[str, Workspace]:
        raise NotImplementedError

    def create(
        self,
        workspace_id: str,
        initial_config: dict | None = None,
    ) -> Workspace:
        raise NotImplementedError

    def destroy(self, workspace_id: str) -> None:
        raise NotImplementedError

    def list_workspaces(self) -> list[Workspace]:
        raise NotImplementedError

    def replace(
        self,
        old_workspace_id: str,
        new_workspace_id: str,
        new_config: dict | None = None,
    ) -> Workspace:
        raise NotImplementedError
