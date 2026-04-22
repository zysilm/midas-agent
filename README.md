# Midas Agent

Budget-driven multi-agent training engine for LLM coding agents. Evolves workflow configurations on SWE-bench Verified through efficiency-based selection.

| Feature | Description |
|---------|-------------|
| **Efficiency Selection** | Selects by `eta = S / C` (score / cost), not just accuracy |
| **Config Evolution** | Evolves DAG workflow configs via DSPy GEPA prompt optimization |
| **Goal-Driven Merge** | Embeds issue context into step prompts with per-step goal tracking |
| **Multi-Workspace** | Parallel competing workspaces with eviction and reproduction |
| **Checkpoint & Resume** | Per-episode checkpoints, crash-safe, resume from any point |
| **SWE-bench Ready** | Outputs `all_preds.jsonl` and reasoning traces for leaderboard submission |

## Quick Start

```bash
# Install
poetry install

# Configure LLM
cat > .midas/config.yaml << EOF
model: openrouter/qwen/qwen3-coder-30b-a3b-instruct
api_key: sk-or-...
EOF

# Train on SWE-bench Verified
midas train --config train_config_evolution.yaml --train-dir my-run

# Resume after crash
midas train --resume .midas/train/my-run/

# Inference
midas infer --artifact .midas/agents/graph_emergence_artifact.json
```

## Training Config

```yaml
initial_budget: 500000        # tokens per workspace per episode
workspace_count: 3            # parallel competing workspaces
n_evict: 2                    # evict worst N per episode
runtime_mode: config_evolution
multiplier_mode: adaptive     # adaptive budget scaling
mult_max: 2.5                 # max budget multiplier
beta: 0.3                     # LLM judge weight
execution_env: docker         # SWE-bench Docker containers
max_tool_output_chars: 100000
max_context_tokens: 32000
```

## CLI Reference

```bash
# Fresh training (all 500 SWE-bench Verified issues)
midas train --config train_config_evolution.yaml

# Train on first N issues
midas train --config train_config_evolution.yaml --issues 30

# Train single issue by index
midas train --config train_config_evolution.yaml --issue-index 0

# Custom training directory
midas train --config train_config_evolution.yaml --train-dir experiment-1

# Resume latest checkpoint
midas train --resume

# Resume specific run
midas train --resume .midas/train/experiment-1/

# Force fresh start (ignore checkpoint)
midas train --config train_config_evolution.yaml --fresh

# Inference with trained artifact
midas infer --model openrouter/qwen/qwen3-coder-30b-a3b-instruct
```

## Training Output

```
.midas/train/<run-name>/
├── checkpoint.json            # Resume metadata
├── train_config.yaml          # Saved training config
├── all_preds.jsonl            # SWE-bench submission format
├── data/                      # Successful execution traces (GEPA dataset)
├── trajs/                     # Per-issue reasoning traces
└── log/
    ├── configs/               # DAG config YAML per episode
    ├── action_logs/           # JSONL action logs per workspace
    ├── patches/               # Git patches per workspace
    └── best_config.yaml       # Best-eta config at training end
```

## Architecture

```
Scheduler          Budget allocation, eta-based selection, eviction
  |
Workspace(s)       Config Evolution: DAG creation, merge, GEPA optimization
  |
ReactAgent         Tool-calling agent loop (bash, str_replace_editor, task_done)
  |
Docker             SWE-bench container per workspace per episode
```

### Episode Loop

1. Clone repo at base commit
2. Allocate budgets (proportional to eta)
3. Execute all workspaces in parallel (Docker containers)
4. Submit patches
5. Evaluate (SWE-bench execution scorer + LLM judge)
6. Post-episode (config creation / GEPA optimization)
7. Evict worst workspaces, seed replacements from best-eta config
8. Save checkpoint + SWE-bench artifacts

### Config Evolution Pipeline

```
Episode 1:  Single-step agent solves issue
            -> Config creation (trace -> summary -> DAG YAML)

Episode 2+: Base DAG + issue -> ConfigMerger -> merged DAG (per-episode)
            -> Execute merged DAG in Docker
            -> Record successful traces

Every 5 successes: GEPA optimizes base DAG prompts
            -> LLM-as-judge metric on successful traces
            -> Pareto selection (accuracy + brevity)
            -> Holdout regression check
            -> Prompt condensation if oversized
```

## SWE-bench Submission

Training outputs `all_preds.jsonl` and `trajs/` compatible with the [swe-bench/experiments](https://github.com/swe-bench/experiments) submission format.

```bash
# After training completes
cp .midas/train/my-run/all_preds.jsonl evaluation/verified/midas_agent/
cp -r .midas/train/my-run/trajs/ evaluation/verified/midas_agent/trajs/
# Add metadata.yaml and README.md per swe-bench checklist
# Submit PR to swe-bench/experiments
```

## Model Support

Tested with:
- `openrouter/qwen/qwen3-coder-30b-a3b-instruct`
- `openrouter/minimax/minimax-m2.5`

Any LiteLLM-compatible model works. Configure in `.midas/config.yaml`.

## Requirements

- Python 3.11+
- Docker (for SWE-bench execution)
- Poetry

## License

MIT
