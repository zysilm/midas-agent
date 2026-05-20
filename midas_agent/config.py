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
    # HTA runtime mode (runtime_mode="hta")
    hta_epsilon: float = 1e-6           # group-advantage std collapse threshold
    hta_novel_threshold: int = 3        # occurrences before a __novel__ slug registers
    hta_max_decision_points: int = 12   # safety cap on decision-graph size per issue
    hta_enable_test_scope: bool = False  # enable the optional test_scope_strategy DP
    # Typed advantage memory — asymmetric EMA learning rates (Clip-Higher analog)
    hta_eta_high: float = 0.30          # EMA step for positive advantages
    hta_eta_low: float = 0.10           # EMA step for negative advantages
