# Midas Agent

A budget-driven multi-agent training engine that evolves workflow configurations for LLM-based coding agents. Trains on real-world GitHub issues (SWE-bench Verified) and learns what workflow patterns work best within tight token budgets.

## Core Idea

Instead of maximizing accuracy alone (max S), Midas selects by **efficiency score** `eta = S / C` — performance divided by token cost. This means cheaper, faster solutions are preferred over brute-force approaches that burn through the budget.

## Architecture

```
Layer 1: SCHEDULER        Budget allocation, evaluation, selection (eta-based)
Layer 2: WORKSPACE         Config Evolution (DAG workflows) or Graph Emergence
Layer 3: STDLIB            ReactAgent + Actions (bash, str_replace_editor, task_done)
```

### Config Evolution Mode

Evolves YAML workflow configurations through a multi-stage pipeline:

1. **Config Creation** — On first successful solve (s_exec=1.0), generates a multi-step DAG config from the execution trace (two-pass LLM: trace -> summary -> YAML)
2. **DAG Merge** — Per episode, embeds issue-specific context into the base DAG step prompts. Each step only sees the issue context relevant to it (localize gets symptoms, investigate gets repro code, fix gets expected behavior)
3. **Goal-Driven Merge** — Extracts a one-sentence GOAL from the issue and embeds it in every step prompt, so the agent never loses sight of what "done" looks like
4. **GEPA Optimization** — Every N episodes, DSPy GEPA evolves step prompts using an LLM-as-judge metric on successful execution traces. Sliding window (last 20 successes), Pareto selection (accuracy + brevity), holdout regression check
5. **Prompt Condensation** — When GEPA produces oversized prompts, asks the system LLM to condense instead of rejecting

### Budget Allocation

- Proportional to eta: higher-scoring, cheaper workspaces get more budget
- Adaptive multiplier (5-zone bang-bang controller) adjusts total pool based on eviction rate
- Eviction via score floor: worst workspaces get replaced with best-eta config

### Multi-Workspace Competition

With `workspace_count > 1`, multiple DAG configs compete on the same issues. Each episode:
1. All workspaces execute in parallel (ThreadPoolExecutor, one Docker container each)
2. Evaluate all patches via SWE-bench execution scorer + LLM judge
3. Select survivors by eta, evict worst, seed replacements from best config

## Training

```bash
# Start training on SWE-bench Verified (500 issues)
midas train --config train_config_evolution.yaml --train-dir my-run

# Resume after crash (auto-detects latest checkpoint)
midas train --resume

# Resume specific run
midas train --resume .midas/train/my-run/
```

### Training Config

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
max_context_tokens: 32000     # triggers compaction at 90%
```

### Training Output

```
.midas/train/<run-name>/
├── checkpoint.json            # Resume metadata
├── train_config.yaml          # Saved training config
├── all_preds.jsonl            # SWE-bench submission format
├── data/                      # GEPA training data (successful traces)
├── trajs/                     # Per-issue reasoning traces (.md)
└── log/
    ├── configs/               # DAG config YAML per episode per workspace
    ├── action_logs/           # JSONL action logs
    ├── patches/               # Git patches
    └── best_config.yaml       # Best eta config at training end
```

### Checkpoint & Resume

Training saves a checkpoint after each episode. On crash, resume picks up where it left off:
- Rebuilds workspace configs from saved YAML
- Reloads GEPA dataset from persisted JSON traces
- Restores adaptive multiplier state
- Skips already-processed issues

## Inference

```bash
midas infer --artifact .midas/agents/graph_emergence_artifact.json
```

## Key Design Decisions

### Why DAG Merge?

Without merge, the agent receives the full issue as a user message before the DAG starts. It sees the bug, knows the answer, and overscopes — e.g., editing code in the "localize" step. With merge, each step only sees its slice of the issue. Localize gets symptoms, investigate gets repro code, fix gets expected behavior. The agent can't skip ahead because it doesn't have the full picture.

### Why GEPA over Reflective Mutation?

The original reflective mutation caused monotonic prompt inflation (2.5KB -> 6KB over 8 rounds) with no performance improvement. GEPA provides:
- Brevity pressure via Pareto selection
- Holdout regression check (won't accept worse prompts)
- Conservative by design — keeps original when data is insufficient

### Why LLM-as-Judge Metric?

Word overlap between ChainOfThought output and execution traces is meaningless. The LLM judge scores strategy alignment ("does the plan follow a similar approach to the successful trace?") and provides actionable feedback for GEPA's reflection LM.

### Why 32K Context Window?

Qwen3-coder-30B supports 128K but performs poorly at the upper end. At 32K, compaction triggers regularly and forces the model to work with a compact, summarized context. This produces better results than letting the context bloat to 100K.

## Model Support

Tested with:
- `openrouter/qwen/qwen3-coder-30b-a3b-instruct`
- `openrouter/minimax/minimax-m2.5`

Configure via `.midas/config.yaml`:
```yaml
model: openrouter/qwen/qwen3-coder-30b-a3b-instruct
api_key: sk-or-...
```

## Dependencies

```bash
poetry install
```

Requires Docker for SWE-bench execution environment.

## SWE-bench Leaderboard Submission

Training automatically generates `all_preds.jsonl` and `trajs/` in the SWE-bench submission format. To submit:

1. Run full training: `midas train --config train_config_evolution.yaml`
2. Copy `all_preds.jsonl`, `trajs/` to the [swe-bench/experiments](https://github.com/swe-bench/experiments) repo
3. Add `metadata.yaml` and `README.md` per their checklist
4. Submit PR
