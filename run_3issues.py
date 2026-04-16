"""Run training on SWE-bench Verified issues through the full pipeline."""
import logging

from midas_agent.config import MidasConfig
from midas_agent.main_training import load_swe_bench, run_training

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)

config = MidasConfig(
    initial_budget=2000000,
    workspace_count=1,
    runtime_mode="graph_emergence",
    n_evict=0,
    score_floor=0.01,
    multiplier_mode="adaptive",
    multiplier_init=1.0,
    beta=0.3,
    model="openrouter/qwen/qwen3-coder-30b-a3b-instruct",
    api_key="REDACTED_API_KEY",
    execution_env="docker",
)

issues = load_swe_bench(split="test")
print(f"Loaded {len(issues)} issues. Running first 2: {[i.issue_id for i in issues[:2]]}")

run_training(config, issues=issues[:2])
