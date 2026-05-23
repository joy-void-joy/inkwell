---
allowed-tools: Bash(uv run lup-devtools:*), Read, Grep, Glob, AskUserQuestion, Skill(lup:commit)
description: Review changes since last bump and bump agent version
argument-hint: [patch|minor|major]
---

# Version Bump

Review changes since the last version bump and bump `AGENT_VERSION` accordingly.

## Input

**Bump level** (optional): $ARGUMENTS

If no level is provided, determine the appropriate level from the changes.

## Process

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before bumping.

### 2. Gather context

```bash
uv run lup-devtools version --json
```

This shows the current version, latest tag, and recent history.

### 3. Classify changes

```bash
uv run lup-devtools version changelog --json
```

Read through the commits and categorize:

- **Behavior changes** (require a bump): prompt changes, new/modified tools, scoring logic, subagent changes
- **Data changes** (no bump needed): session outputs, notes, resolution updates
- **Infrastructure changes** (no bump needed): dependencies, CI, scripts, CLAUDE.md

If there are NO behavior changes since the last bump, inform the user and stop.

### 4. Determine bump level

| Level     | When                                          | Examples                                     |
| --------- | --------------------------------------------- | -------------------------------------------- |
| **patch** | Bug fixes, config tweaks, tool fixes          | Fixed API error handling, adjusted timeout   |
| **minor** | Prompt changes, new tools, tool modifications | Added web search tool, rewrote system prompt |
| **major** | Architecture changes                          | New LLM, new framework, fundamental redesign |

If the user provided a level in `$ARGUMENTS`, use it. Otherwise, recommend and confirm via AskUserQuestion.

### 5. Apply the bump

```bash
uv run lup-devtools version bump <level>
```

### 6. Report

Show the user what was bumped and the behavioral changes that warranted it.

## Guidelines

- **Only bump for behavior changes** -- Data, docs, and infra commits don't warrant a bump
- **Summarize what changed for the agent**, not the codebase
- **When in doubt, ask** -- Use AskUserQuestion if the level is ambiguous