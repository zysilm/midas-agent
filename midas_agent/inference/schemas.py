"""Pydantic schemas for production mode artifact serialization."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SoulSchema(BaseModel):
    system_prompt: str


class SkillSchema(BaseModel):
    name: str
    description: str
    content: str


class ResponsibleAgentSchema(BaseModel):
    soul: SoulSchema
    skill: SkillSchema | None = None


class FreeAgentSchema(BaseModel):
    agent_id: str
    soul: SoulSchema
    skill: SkillSchema | None = None
    price: int
    bankruptcy_rate: float = Field(ge=0.0, le=1.0)


class GraphEmergenceArtifact(BaseModel):
    """Production artifact for Graph Emergence mode."""
    responsible_agent: ResponsibleAgentSchema
    free_agents: list[FreeAgentSchema] = Field(default_factory=list)
    budget_hint: int
