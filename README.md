# Midas Agent

A self-improving coding agent that learns from its own failures. Given a set of GitHub issues, Midas trains a multi-step DAG workflow through a closed-loop process: solve issues, analyze failures, reflect on what went wrong, and evolve the workflow prompts — so the next batch of issues benefits from past mistakes.

## Motivation

Most coding agents use a fixed prompt and hope for the best. When they fail, the failure is discarded. Midas closes that loop:

1. The agent solves issues using a **multi-step DAG** (localize → investigate → fix → validate)
2. Failed attempts are **analyzed** — an LLM identifies which step went wrong and extracts an abstract lesson
3. A **reflector** rewrites the DAG prompts to incorporate those lessons
4. The new config is **validated head-to-head** against the old one on fresh issues
5. The winner survives. Repeat.

Over episodes, the DAG prompts evolve from generic instructions into battle-tested guidance like *"don't edit test files"*, *"fix the error message, not the condition logic"*, *"actually change the behavior, don't just add a deprecation warning."*

## Pipeline

### 1. Training Loop (per issue)

Each episode takes one SWE-bench issue and runs it through the current DAG config:

```mermaid
flowchart LR
    subgraph episode["Episode (one issue)"]
        direction LR
        A["Issue<br/>from SWE-bench"] --> B["ConfigMerger<br/><i>embed issue into<br/>step prompts</i>"]
        B --> C["DAG Executor<br/><i>step 1 → step 2 → ... → step N</i><br/><i>StepJudge validates each</i>"]
        C --> D["Patch"]
        D --> E["SWE-bench<br/>Scorer"]
        E --> F{pass?}
        F -->|"score=1"| G["Record<br/>success trace"]
        F -->|"score=0"| H["Record failure<br/>trace + patch +<br/>gold test names"]
    end

    style A fill:#0d1117,stroke:#58a6ff,color:#fff
    style B fill:#0d1117,stroke:#58a6ff,color:#fff
    style C fill:#0d1117,stroke:#58a6ff,color:#fff
    style D fill:#0d1117,stroke:#58a6ff,color:#fff
    style E fill:#0d1117,stroke:#58a6ff,color:#fff
    style F fill:#0d1117,stroke:#f0883e,color:#fff
    style G fill:#0d1117,stroke:#3fb950,color:#fff
    style H fill:#0d1117,stroke:#f85149,color:#fff
```

### 2. Config Evolution (every N episodes)

After N episodes, the accumulated traces trigger the evolution cycle — this is the **closed loop**:

```mermaid
flowchart TD
    A["Accumulated Traces<br/><i>success traces + failure traces</i>"] --> B
    B["Failure Analyzer<br/><i>for each failure:</i><br/><i>which step went wrong?</i><br/><i>what is the abstract lesson?</i>"] --> C
    C["Config Reflector<br/><i>sees all traces + lessons</i><br/><i>rewrites DAG step prompts</i><br/><i>lessons condensed, not appended</i>"] --> D
    D["New Config<br/>(candidate)"] --> E
    E["Head-to-Head<br/><i>champion vs candidate</i><br/><i>run on same future issues</i>"] --> F
    F{candidate wins?}
    F -->|"yes"| G["Candidate becomes<br/>new champion"]
    F -->|"no"| H["Keep current<br/>champion"]
    G --> I["Next N episodes<br/><i>using improved config</i>"]
    H --> I
    I -->|"accumulate more traces"| A

    style A fill:#0d1117,stroke:#58a6ff,color:#fff
    style B fill:#0d1117,stroke:#f85149,color:#fff
    style C fill:#0d1117,stroke:#f85149,color:#fff
    style D fill:#0d1117,stroke:#f0883e,color:#fff
    style E fill:#0d1117,stroke:#3fb950,color:#fff
    style F fill:#0d1117,stroke:#f0883e,color:#fff
    style G fill:#0d1117,stroke:#3fb950,color:#fff
    style H fill:#0d1117,stroke:#58a6ff,color:#fff
    style I fill:#0d1117,stroke:#58a6ff,color:#fff
```

The first diagram runs hundreds of times. The second diagram triggers periodically and feeds an improved config back into the first — forming the closed loop.

## Quick Start

```bash
poetry install

# Configure LLM (any LiteLLM-compatible provider)
cat > .midas/config.yaml << EOF
model: minimax/MiniMax-M2.5
api_key: sk-...
api_base: https://api.minimax.io/v1
EOF

# Train (evolves DAG config over episodes)
midas train --config train_config_evolution.yaml --issues 30

# Resume from checkpoint
midas train --resume .midas/train/my-run/

# Eval with frozen config (no evolution)
midas infer --dag .midas/train/my-run/log/configs/ws-0_ep10.yaml --issues 50

# Interactive mode
midas infer --dag config.yaml
```

## Key Features

- **Closed-loop learning** — failures are analyzed, lessons extracted, prompts improved
- **DAG workflows** — multi-step plans that evolve from generic to battle-tested
- **Adaptive workspaces** — champion vs challenger, winner survives
- **No task_done tool** — text response = done; unknown tool calls treated as termination
- **ConfigMerger** — embeds issue into step prompts to prevent overscoping
- **Rich failure analysis** — sees full trace, patch diff, and gold test names
- **Checkpoint & resume** — per-episode snapshots, crash-safe

## Training Output

```
.midas/train/<run>/
├── checkpoint.json
├── train_config.yaml
├── all_preds.jsonl          # SWE-bench submission
├── data/                    # Success + failure traces (GEPA dataset)
└── log/configs/             # DAG YAML per episode (shows prompt evolution)
```
