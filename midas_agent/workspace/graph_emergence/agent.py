"""Agent and Soul for Graph Emergence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from midas_agent.workspace.graph_emergence.skill import Skill


@dataclass
class Soul:
    system_prompt: str


@dataclass
class Agent:
    agent_id: str
    soul: Soul
    agent_type: str  # "workspace_bound" | "free"
    skill: Skill | None = None
    protected_by: str | None = None
    protecting: list[str] = field(default_factory=list)
