---
allowed-tools: Bash(uv run lup-devtools:*), Read, Grep, Glob, Agent, AskUserQuestion
description: Deep trace reading and error classification for selected sessions
argument-hint: <session_id1> [session_id2 ...]
---

# Investigate: Trace Deep-Dive

Build first-hand understanding of what happened in each target session.

## Per-Session Investigation

For each target session:

### 1. Read the trace

```bash
uv run lup-devtools trace show <session_id>
```

Read every tool call, error, and reasoning step. Don't skim.

For a focused view of tool interactions:

```bash
uv run lup-devtools trace show <session_id> --tool-calls
```

### 2. Tool use audit

- **Tool call inventory**: List every tool call — what the agent tried to learn, whether it succeeded, whether the result was useful.
- **Tool errors**: For each failure — what happened (quote the error), why it failed (read the tool source in `src/lup_template/agent/tools/`), was recovery reasonable.
- **Subtle bugs**: Cases where a tool *succeeded* but returned misleading or incomplete data.
- **Missing tool calls**: Tools the agent *should* have called but didn't. Check available tools in `src/lup_template/agent/tools/`.

### 3. Workflow assessment

- **Information gathering**: Enough evidence? Diverse sources? Or jumped to conclusions?
- **Structured reasoning**: Decomposed the problem? Weighed uncertainties?
- **Self-correction**: Updated views on new evidence? Flagged its own uncertainty?
- **Efficiency**: Wasted tool calls? Proportional effort?

### 4. Pipeline health

System-level problems separate from agent reasoning:
- MCP connection issues (tools timing out, empty results)
- Token/context pressure (reasoning truncated, limits hit)
- Prompt issues (agent confused by instructions)
- Hook behavior (permission hooks blocking valid operations)

Read relevant source code when you spot a pipeline issue.

### 5. Classify the outcome

| Type | Description |
|------|-------------|
| Good outcome | Correct approach, reasonable result |
| Missing capability | Agent lacked a tool or data source it needed |
| Tool failure | Available tool broke or returned bad data |
| Reasoning error | Agent had the data but drew wrong conclusions |
| Scope misunderstanding | Misinterpreted the task requirements |
| Efficiency issue | Got there but wasted significant effort |

For each issue, build a **counterfactual**: what specific tool, data source, or reasoning step would have changed the outcome?

## Gate

Use AskUserQuestion to present:
1. Per-session summary table: Session ID | Task | Outcome Type | Key Finding
2. Cross-session patterns (if multiple sessions)
3. Top 2-3 counterfactuals

Options: "Proceed to analysis" / "Dig deeper on specific sessions" / "Skip to implementation"