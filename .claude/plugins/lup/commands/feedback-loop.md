---
allowed-tools: Bash(git:*, uv run lup-devtools:*, uv run python -m lup:*), Read, Grep, Glob, Edit, Write, Task, WebSearch, WebFetch, AskUserQuestion
description: Full feedback loop — orchestrates status, investigation, analysis, reflection, and implementation
argument-hint: [optional: paste a trace, reflection, or output for single-trace analysis]
---

# Feedback Loop Orchestrator

Run the full feedback loop by invoking subcommands in sequence. Each subcommand is independently invocable — the orchestrator calls them in order with gates between phases.

## Single-Trace Mode

**If the user pasted trace content as an argument**: $ARGUMENTS

When trace content is provided, run a focused single-trace deep analysis using the `/fb-investigate` process (Steps 1-5) on the pasted content, then stop. Do not proceed to the full loop.

## Three Levels of Analysis

- **Object Level**: The agent itself — tools, capabilities, runtime behavior
- **Meta Level**: The agent's self-assessment — is tracking data accurate? Is the reflection schema capturing what matters?
- **Meta-Meta Level**: This feedback loop process — are the subcommands useful? Are the devtools providing the right data?

A good feedback loop session produces changes at multiple levels. If you only made object-level changes, you probably skipped the reflection phases.

## Guiding Principle: The Bitter Lesson

**Tools are the highest-leverage change you can make.** When the agent struggles, the answer is almost always a missing tool — not a missing prompt paragraph.

| Prefer | Over |
|--------|------|
| Tools that provide data | Prompt rules that constrain behavior |
| General principles | Specific pattern patches |
| State/context via tools | F-string prompt engineering |
| Subagents for specialized work | Complex pipelines in main agent |

**The test**: Would this change still help if the domain shifted completely?

## Sequence

### 1. `/fb-status` — State + targets

Pass $ARGUMENTS through. Ends with a gate — confirm targets before proceeding.

### 2. `/fb-investigate` — Deep trace reading

Read and analyze the selected sessions deeply. Ends with a gate — confirm findings before proceeding.

### 3. `/fb-analyze` — Tool health + capability gaps + patterns

Aggregate findings from metrics and traces to identify systemic patterns.

### 4. `/fb-reflect` — Meta + meta-meta reflection

Is the agent tracking enough data? Is this feedback loop working? Update subcommands and devtools as needed.

### 5. `/fb-implement` — Make changes + queue evaluation

Implement prioritized changes (tools first, prompts last). Bump version. Queue evaluation sessions.

## Documentation

Write analysis output to `notes/feedback_loop/<timestamp>_analysis.md`:

```markdown
# Feedback Loop Analysis: YYYY-MM-DD

## Ground Truth Status
- Agent version analyzed: X.Y.Z
- Sessions analyzed: N
- Sessions with outcomes: N

## Object-Level Findings

### Tool Failures
| Tool | Failure | Count | Fix |
| ---- | ------- | ----- | --- |

### Capability Requests
- "Would benefit from X" → [action taken]

### Reasoning Quality
- [Assessment for key sessions]

## Meta-Level Findings
- Was tracking data sufficient?
- What data was missing?

## Meta-Meta Findings
- Updates to subcommands or devtools

## Changes Made
| Level | Change | Rationale |
| ----- | ------ | --------- |

## Evaluation Queue
uv run python -m lup_template.environment.cli loop "task1" "task2" "task3"
```

## Key Questions

1. **What AGENT_VERSION am I analyzing?** Filter ALL data by version.
2. **Do we have outcome data?** If no, focus on process not accuracy.
3. **What tools fail repeatedly?** Fix or replace them.
4. **What does the agent say it needs?** Trust and provide.
5. **Is the prompt accumulating patches?** Use `uv run lup-devtools feedback prompt-health` to check.