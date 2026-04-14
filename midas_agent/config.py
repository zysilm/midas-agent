"""Global configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MidasConfig:
    initial_budget: int
    workspace_count: int
    runtime_mode: str
    score_floor: float = 0.01
    multiplier_mode: str = "static"
    multiplier_init: float = 1.0
    er_target: float = 0.0
    cool_down: int = 0
    mult_min: float = 0.5
    mult_max: float = 2.0
    beta: float = 0.3
    eval_model: str = ""
    n_evict: int = 1
    max_iterations_free_agent: int = 50
    storage_backend: str = "sqlite"
