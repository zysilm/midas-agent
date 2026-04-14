"""Configuration schema for Configuration Evolution workflows."""
from dataclasses import dataclass, field


@dataclass
class ConfigMeta:
    name: str
    description: str


@dataclass
class StepConfig:
    id: str
    prompt: str
    tools: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)


@dataclass
class WorkflowConfig:
    meta: ConfigMeta
    steps: list[StepConfig] = field(default_factory=list)
