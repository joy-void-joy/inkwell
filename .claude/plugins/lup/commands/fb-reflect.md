---
allowed-tools: Bash(uv run lup-devtools:*), Read, Grep, Glob, Edit, Write, AskUserQuestion
description: Meta and meta-meta reflection on the feedback loop process itself
---

# Reflect: Process Quality Assessment

Two levels of reflection: is the agent tracking enough data (meta), and is the feedback loop itself working (meta-meta)?

## Meta Level

### 1. Tracking data quality

Is the agent emitting enough data for analysis? Check coverage of: traces, reflection outputs, tool metrics, session results.

```bash
uv run lup-devtools feedback status
uv run lup-devtools trace list
```

### 2. Actionable insight check

Did this session surface specific, actionable improvements? If findings are circular ("agent should be better at X" without a concrete tool/capability to build), the tracking or review process needs improvement.

### 3. Missing data

Common gaps:
- **No outcome data** → Can't evaluate accuracy, only process
- **No tool-to-session linkage** → Can't identify which tools help
- **No category tagging** → Can't identify patterns by type

## Meta-Meta Level

### 4. Prompt health

Read the full system prompt (`src/lup_template/agent/prompts.py`). Is it accumulating patches? Count conditional exceptions added since the last rewrite. If >3, flag for structural rewrite.

### 5. Subcommand assessment

Were the `/fb-*` subcommands helpful? Anything confusing, missing, or redundant? Update the subcommand files with learnings.

### 6. Devtools assessment

Any repetitive analysis that should be automated as a devtools command? Add commands to `src/lup_template/devtools/`.