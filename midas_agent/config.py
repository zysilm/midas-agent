"""Global configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MidasConfig:
    initial_budget: int
    runtime_mode: str
    workspace_count: int = 1
    score_floor: float = 0.01
    multiplier_mode: str = "adaptive"
    multiplier_init: float = 1.0
    er_target: float = 0.1
    cool_down: float = 0.05
    mult_min: float = 0.5
    mult_max: float = 50.0
    beta: float = 0.3
    n_evict: int = 0
    # Adaptive workspace mode: 1 workspace normally, 2 during head-to-head
    adaptive_workspaces: bool = False
    max_workspaces: int = 1
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
    # Context management
    max_tool_output_chars: int = 100000
    max_context_tokens: int = 32000
    temperature: float = 0.0
    top_p: float = 1.0
    skill_evolution: bool = True
    # Lesson retrieval: minimum cosine similarity to inject a lesson
    lesson_similarity_threshold: float = 0.50
