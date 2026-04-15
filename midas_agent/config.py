"""Global configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MidasConfig:
    initial_budget: int
    workspace_count: int
    runtime_mode: str
    score_floor: float = 0.01
    multiplier_mode: str = "adaptive"
    multiplier_init: float = 1.0
    er_target: float = 0.1
    cool_down: float = 0.05
    mult_min: float = 0.5
    mult_max: float = 5.0
    beta: float = 0.3
    n_evict: int = 1
    max_iterations_free_agent: int = 50
    storage_backend: str = "sqlite"
    # Task execution LLM (empty model = stub)
    model: str = ""
    api_key: str = ""
    api_base: str = ""
    # Evaluation LLM judge (empty = same as task LLM)
    eval_model: str = ""
    eval_api_key: str = ""
    eval_api_base: str = ""
    # Execution environment: "local" = current behavior,
    # "docker" = bash commands run inside SWE-bench Docker container
    execution_env: str = "local"
