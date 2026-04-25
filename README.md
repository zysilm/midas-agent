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

<p align="center">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 520" width="800" height="520" font-family="system-ui, sans-serif" font-size="13">
  <defs>
    <marker id="ah" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6Z" fill="#888"/></marker>
  </defs>

  <!-- Background label: CLOSED LOOP -->
  <text x="400" y="505" text-anchor="middle" fill="#555" font-size="12" font-style="italic">closed-loop training — each cycle improves the next</text>

  <!-- 1. Training Loop -->
  <rect x="280" y="15" width="240" height="50" rx="10" fill="#1a1a2e" stroke="#e94560" stroke-width="2"/>
  <text x="400" y="38" text-anchor="middle" fill="#fff" font-weight="bold" font-size="14">Training Loop</text>
  <text x="400" y="54" text-anchor="middle" fill="#aaa" font-size="11">for each SWE-bench issue</text>

  <!-- 2. DAG Executor -->
  <rect x="560" y="110" width="220" height="70" rx="10" fill="#1a1a2e" stroke="#0f3460" stroke-width="2"/>
  <text x="670" y="135" text-anchor="middle" fill="#fff" font-weight="bold">DAG Executor</text>
  <text x="670" y="153" text-anchor="middle" fill="#aaa" font-size="10">localize → investigate → fix → validate</text>
  <text x="670" y="168" text-anchor="middle" fill="#aaa" font-size="10">StepJudge validates each transition</text>

  <!-- 3. SWE-bench Scorer -->
  <rect x="560" y="230" width="220" height="50" rx="10" fill="#1a1a2e" stroke="#0f3460" stroke-width="2"/>
  <text x="670" y="260" text-anchor="middle" fill="#fff" font-weight="bold">SWE-bench Scorer</text>

  <!-- 4. Failure Analyzer -->
  <rect x="280" y="340" width="240" height="70" rx="10" fill="#1a1a2e" stroke="#e94560" stroke-width="2"/>
  <text x="400" y="365" text-anchor="middle" fill="#fff" font-weight="bold">Failure Analyzer</text>
  <text x="400" y="383" text-anchor="middle" fill="#aaa" font-size="10">sees: trace + patch + gold test names</text>
  <text x="400" y="398" text-anchor="middle" fill="#aaa" font-size="10">outputs: which step failed + abstract lesson</text>

  <!-- 5. GEPA Reflector -->
  <rect x="20" y="230" width="220" height="70" rx="10" fill="#1a1a2e" stroke="#e94560" stroke-width="2"/>
  <text x="130" y="255" text-anchor="middle" fill="#fff" font-weight="bold">Config Reflector</text>
  <text x="130" y="273" text-anchor="middle" fill="#aaa" font-size="10">success traces + failure lessons</text>
  <text x="130" y="288" text-anchor="middle" fill="#aaa" font-size="10">→ rewrites all step prompts</text>

  <!-- 6. Adaptive Workspace -->
  <rect x="20" y="110" width="220" height="70" rx="10" fill="#1a1a2e" stroke="#16c79a" stroke-width="2"/>
  <text x="130" y="135" text-anchor="middle" fill="#fff" font-weight="bold">Adaptive Workspace</text>
  <text x="130" y="153" text-anchor="middle" fill="#aaa" font-size="10">champion vs challenger (head-to-head)</text>
  <text x="130" y="168" text-anchor="middle" fill="#aaa" font-size="10">winner selected by issues solved</text>

  <!-- Arrows -->
  <path d="M520,45 Q600,45 600,110" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#ah)"/>
  <text x="575" y="78" fill="#aaa" font-size="10">issue + merged config</text>

  <path d="M670,180 L670,230" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#ah)"/>
  <text x="685" y="210" fill="#aaa" font-size="10">patch</text>

  <path d="M560,260 Q400,310 400,340" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#ah)"/>
  <text x="460" y="310" fill="#aaa" font-size="10">score=0 → analyze</text>

  <path d="M280,380 Q130,420 130,300" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#ah)"/>
  <text x="160" y="370" fill="#aaa" font-size="10">lessons</text>

  <path d="M130,230 L130,180" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#ah)"/>
  <text x="85" y="210" fill="#aaa" font-size="10">new config</text>

  <path d="M240,130 Q280,90 280,55" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#ah)"/>
  <text x="215" y="78" fill="#aaa" font-size="10">champion config</text>

  <!-- Success path shortcut -->
  <path d="M560,245 Q460,245 460,65" fill="none" stroke="#16c79a" stroke-width="1.2" stroke-dasharray="5,3" marker-end="url(#ah)"/>
  <text x="470" y="160" fill="#16c79a" font-size="10">score=1 → record</text>
</svg>
</p>

### How the loop works

| Step | What happens |
|------|-------------|
| **Train** | Pick an issue, merge it into the DAG step prompts, run in Docker |
| **Execute** | Agent follows DAG steps. Text response = step done. StepJudge validates. |
| **Score** | SWE-bench runs gold tests. Pass (1.0) or fail (0.0). |
| **Analyze** | On failure: LLM sees full trace + agent's patch + gold test names. Identifies which step failed and extracts an abstract lesson. |
| **Reflect** | Every N episodes: ConfigReflector sees all success + failure traces. Rewrites DAG prompts — lessons are condensed in, not appended. |
| **Compete** | New config enters head-to-head against champion on fresh issues. Winner keeps its spot. |

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

## License

MIT
