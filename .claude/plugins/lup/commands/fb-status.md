---
allowed-tools: Bash(uv run lup-devtools:*), Read, Grep, Glob, AskUserQuestion
description: Feedback loop entry point — status, targets, and previous session context
---

# Status: Feedback Loop Entry Point

Get the current state of the agent and select analysis targets.

## Process

### 1. Agent version and data overview

```bash
uv run lup-devtools version
uv run lup-devtools feedback status
```

### 2. Previous session

Read the most recent analysis file in `notes/feedback_loop/`:

```bash
ls -t notes/feedback_loop/*_analysis.md 2>/dev/null | head -1
```

If it exists, read it. Note what was already fixed — don't re-investigate.

### 3. Select targets

Find sessions to analyze:

```bash
uv run lup-devtools feedback unanalyzed
uv run lup-devtools feedback errors
```

Prioritize: sessions with errors, sessions with poor outcomes (if outcome data exists), recent sessions from the current version.

### 4. Gate

Use AskUserQuestion to present:
- Agent version and session count
- Selected target sessions with key stats
- What was done last session (if applicable)

Options: "Proceed with these targets" / "Change target selection" / "Custom"