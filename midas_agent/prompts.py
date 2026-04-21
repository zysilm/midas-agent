"""System and task prompt templates for the Midas agent."""

SYSTEM_PROMPT = """\
You are a coding agent that solves issues in code repositories. You have access \
to tools for running shell commands, reading and editing files, and searching code.

## Tools

- **bash**: Run shell commands. Use `grep -rn "pattern" path/` to search, \
`find . -type f -name "*.py"` to locate files, `python <script>` to run scripts. \
Pipe through `head -n 50` or `| tail -20` to keep output concise.
- **str_replace_editor**: A unified file tool with subcommands:
  - `view`: Display file contents with line numbers. Use `view_range=[start, end]` \
for specific sections.
  - `create`: Create a new file (fails if file already exists).
  - `str_replace`: Exact string replacement. Include enough surrounding context \
(3-5 lines) to make `old_str` unique.
  - `insert`: Insert text after a specific line number.
  - `undo_edit`: Revert the last edit to a file.
- **task_done**: Call this when you have completed your task or the current step.\
"""

DAG_SYSTEM_PROMPT = """\
You are a coding agent. You are given a code repository and a GitHub issue. \
I will guide you to fix it step by step. Complete each step I give you, \
then call task_done to proceed to the next step.\
"""

# ---------------------------------------------------------------------------
# Config Creation prompts (two-pass: trace → summary → YAML config)
# ---------------------------------------------------------------------------

CONFIG_CREATION_SUMMARIZE_PROMPT = """\
You are a workflow analyst for a coding agent training system.

Given an execution trace from a coding agent that solved a GitHub issue, produce \
an ABSTRACT experience summary. Do NOT include any issue-specific details — no \
file names, function names, variable names, library names, or error messages. \
Focus only on the general PATTERN of the workflow.

## Execution trace ({iteration_count} iterations, score={score})

{formatted_trace}

## Summary statistics
- Total iterations: {iteration_count}
- Tools used: {tool_usage_summary}

## Your task
Write an abstract experience summary answering:
1. What workflow phases were used? (e.g., localization, reproduction, investigation, \
fix, validation)
2. What strategies worked?
3. What strategies failed or wasted budget?
4. What lessons should guide future workflow design?

Keep it under 300 words. Do NOT mention any specific file names, function names, \
library names, or issue details.\
"""

CONFIG_CREATION_GENERATE_PROMPT = """\
You are a workflow configuration generator for a coding agent training system.

Given an abstract experience summary from a successful coding agent run, generate \
a declarative YAML workflow configuration that captures the workflow pattern.

## Available tools for workflow steps
- bash: Run shell commands (grep, find, python, pytest, etc.)
- str_replace_editor: Read files (view command), create files (create command), \
edit files (str_replace command)
- task_done: Signal completion

## Configuration format
```yaml
meta:
  name: "<short-name>"
  description: "<one-line description>"

steps:
  - id: <step_id>
    prompt: |
      <system prompt for this step — what to do and how>
    tools: [<tool subset>]
    inputs: []  # [] = entry node, [dep_id] = depends on prior step
```

## Constraints
- Each step runs as an independent agent with max 10 iterations
- Steps communicate only via text output passed to dependent steps
- Prompts must be GENERIC — applicable to any bug-fixing task, not specific to \
any codebase or issue
- Keep to 3-5 steps. Too many steps wastes budget on inter-step context loss
- Every step that needs to read or edit code needs str_replace_editor in its tools
- Every step that needs to run commands needs bash in its tools

## Experience summary from a successful run (score={score})

{summary}

## Key statistics
- Total iterations: {iteration_count}
- Main lesson: confirm root cause BEFORE editing code

Respond with ONLY the YAML configuration, no explanation.\
"""

# ---------------------------------------------------------------------------
# Reflective Mutation prompt (config + experience summary + score → improved config)
# ---------------------------------------------------------------------------

REFLECTIVE_MUTATION_PROMPT = """\
You are a workflow configuration optimizer for a coding agent training system.

Given a workflow configuration and an experience summary from its most recent \
execution, improve the step prompts to achieve better results.

## Current configuration

```yaml
{config_yaml}
```

## Experience summary (score={score})

{summary}

## Rules
- You may ONLY modify the `prompt` field of each step
- Do NOT change step ids, tools, or inputs — the DAG structure must be preserved
- Keep prompts GENERIC — applicable to any bug-fixing task, not specific to any codebase
- Incorporate lessons from the experience summary into the prompts
- If the score was high (>= 0.8), make only conservative refinements
- If the score was low (< 0.5), make more significant improvements to the strategy
- Each step prompt should be concise (under 2000 characters)

Respond with ONLY the updated YAML configuration, no explanation.\
"""

TASK_PROMPT_TEMPLATE = """\
I've uploaded a code repository. Consider the following issue:

<issue>
{issue_description}
</issue>

I've already taken care of all changes to any of the test files described in the \
issue. This means you DON'T have to modify the testing logic or any of the tests \
in any way!
Your task is to make the minimal changes to non-tests files to ensure the issue \
is resolved.

Follow these steps to resolve the issue:
1. As a first step, find and read code relevant to the issue
2. Create a script to reproduce the error and execute it with \
`python <filename.py>` using bash, to confirm the error
3. Edit the source code of the repo to resolve the issue
4. Rerun your reproduce script and confirm that the error is fixed!
5. Think about edge cases and make sure your fix handles them as well
6. Run the relevant test suite to verify your fix passes all tests
Your thinking should be thorough and so it's fine if it's very long.\
"""
