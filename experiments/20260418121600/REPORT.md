# Experiment Report: Agent Design Comparison (10 SWE-bench Issues)

**Date:** 2026-04-18
**Branch:** spike/improve-agent-design
**Issues:** First 10 from SWE-bench Verified (astropy subset)

## Configurations

| Config | Agent | Model | Budget | Context |
|--------|-------|-------|--------|---------|
| Midas+qwen | Midas (spike) | openrouter/qwen/qwen3-coder-30b-a3b-instruct | 1M tokens | 100K chars, 32K tokens |
| Midas+minimax | Midas (spike) | openrouter/minimax/minimax-m2.5 | 1M tokens | 100K chars, 32K tokens |
| SWE-agent+qwen | SWE-agent v1.1.0 | openrouter/qwen/qwen3-coder-30b-a3b-instruct | unlimited | 16K chars |
| SWE-agent+minimax | SWE-agent v1.1.0 | openrouter/minimax/minimax-m2.5 | unlimited | 16K chars |

## Results

| # | Issue | Midas+minimax | SWE-agent+minimax | Midas+qwen | SWE-agent+qwen |
|---|-------|---------------|-------------------|------------|----------------|
| 1 | astropy-12907 | **Resolved** | **Resolved** | **Resolved** | **Resolved** |
| 2 | astropy-13033 | No | No | No | No |
| 3 | astropy-13236 | No | No | No | No |
| 4 | astropy-13398 | No | No | No | No |
| 5 | astropy-13453 | **Resolved** | **Resolved** | **Resolved** | **Resolved** |
| 6 | astropy-13579 | **Resolved** | **Resolved** | No | **Resolved** |
| 7 | astropy-13977 | No | No | No | No |
| 8 | astropy-14096 | No | **Resolved** | No | No |
| 9 | astropy-14182 | **Resolved** | No | No | No |
| 10 | astropy-14309 | No | **Resolved** | No | **Resolved** |
| | **Total** | **4/10 (40%)** | **5/10 (50%)** | **2/10 (20%)** | **4/10 (40%)** |

## Key Findings

### Model quality is the dominant factor
- minimax-m2.5 doubles Midas resolve rate (20% -> 40%) and improves SWE-agent (40% -> 50%)
- Same agent design, different model = 2x improvement

### SWE-agent's simpler design has an edge
- With same model, SWE-agent consistently resolves 1 more issue than Midas
- SWE-agent uses 3 tools (bash, str_replace_editor, submit); Midas uses 9
- SWE-agent has a submit review gate that forces patch cleanup

### Each config solves unique issues
- Midas+minimax uniquely solved 14182 (no other config did)
- SWE-agent+minimax uniquely solved 14096
- No single config dominates all issues

### Non-determinism is significant
- Same model+agent can resolve different issues across runs
- Midas+qwen 100K context solved issues {12907, 13453}
- Midas+qwen 16K context solved issues {12907, 13236} (different set!)

## Spike Changes Applied

1. Forward parameter descriptions/enum/items to LLM tool schema
2. EnvironmentContext (XML) replacing market_info_provider
3. Sub-agents inherit parent system prompt + env context + max_iterations=20
4. update_plan tool (structured planning, replacing think)
5. SWE-bench scorer rewritten for in-memory evaluation (no file caching)
6. Stale Docker container cleanup before evaluation
7. Graceful handling of malformed tool calls and missing parameters
8. Detailed system prompt with approach guidelines, tool tips, common mistakes

## Files

- `midas_qwen/` — Midas training log with Qwen model
- `midas_minimax/` — Midas training log with minimax model
- `sweagent_qwen/` — SWE-agent run log with Qwen model
- `sweagent_minimax/` — SWE-agent run log with minimax model
- `patches/` — All generated patches from all 4 configurations
- `midas_*/artifact.json` — Graph emergence training artifacts
