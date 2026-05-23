---
allowed-tools: Read, Grep, Glob, Bash(ls:*, uv run lup-devtools:*), Task, WebSearch, AskUserQuestion
description: Audit SDK agent tools and subagents — find gaps, overlaps, and refactoring opportunities
---

# Tool Review: Tools, Servers & Pipeline

Review all MCP servers, tools, and subagents. Draw an outline of the pipeline and each tool, then assess: does it make sense? What's good? What's missing?

## Phase 1: Pipeline Overview

Before cataloging individual tools, map the **end-to-end flow**:

1. Read `src/lup_template/agent/core.py` to understand:
   - How a request enters the agent
   - Which MCP servers are configured and how they're launched
   - How tools are exposed to the agent (in-process vs remote)
   - How subagents are spawned and what tools they receive
   - How the final output is produced and returned

2. Read `src/lup_template/agent/tool_policy.py` to understand:
   - Which tools are gated by conditions (API keys, modes, etc.)
   - Are there tools registered but conditionally unavailable?

3. Draw a pipeline diagram (ASCII or markdown) showing:
   ```
   Input → Agent → [MCP Servers / Tools / Subagents] → Output
   ```
   Show which servers provide which tools, how subagents relate to the main agent, and where data flows between components.

## Phase 2: Tool Inventory

Read every file in `src/lup_template/agent/tools/` and build a **tool outline** — not just a table, but a readable summary of each tool's purpose.

For each tool file:
1. Read the file
2. List every `@tool`-decorated function with its name and a one-line description
3. Note which MCP server it belongs to (look for `create_mcp_server` calls)
4. Note external dependencies (APIs, config keys, packages)

For remote/external MCP servers:
- Read `src/lup_template/agent/core.py` for `McpServerConfig` or server lists
- Read `src/lup_template/agent/config.py` for MCP-related settings
- Check `pyproject.toml` for MCP server dependencies
- Grep for `npx`, `uvx`, or other MCP server launch patterns

Present as a grouped outline:

```
Server: main
  - tool_name — What it does in one sentence
  - tool_name — What it does in one sentence

Server: reflect
  - tool_name — ...

Remote: external-service
  - tool_name — ...

Subagents:
  - How they're created, what tools they get, what they're used for
```

## Phase 3: Assessment — Does It Make Sense?

With the pipeline and inventory in hand, assess the design:

### What's Good

- Which tools are well-designed and at the right abstraction level?
- Where does the organization match the domain well?
- Are there tools that clearly earn their place (high value, clean interface)?

### What's Questionable

- Are there tools at the wrong level of granularity (too coarse or too fine)?
- Does the implementation match the interface? (Read the actual code — does the tool deliver what its docstring promises?)
- Are tool names consistent and self-explanatory?
- Do input/output patterns follow consistent conventions?
- Do tools use the shared response helpers (`mcp_error`, `mcp_success`)?
- Is anything in the wrong server or confusingly organized?

### What's Missing

Reason from first principles — given the agent's domain, what capabilities are absent?
- Data sources not covered
- Reasoning tools that could help (statistical computation, simulation, etc.)
- Workflow capabilities missing (caching, batching, etc.)
- Does the agent's system prompt reference capabilities that don't exist as tools?

### What's Redundant

Read the implementations side by side:
- Do any tools hit the same API or compute the same thing with different parameters?
- Are there tools whose functionality is a subset of another tool?
- Are there tools better served by an external MCP server (or vice versa)?

**Optional validation:** Check trace data **filtered to current version** for zero-call tools or co-occurrence patterns — but only report issues confirmed in the current code.

## Phase 4: Report

Present findings as:

```markdown
## Tool Review

### Pipeline
[ASCII diagram showing the flow]

### Tool Outline
[Grouped list from Phase 2]

### What Works Well
- [Strengths of the current design]

### What's Questionable
- [Issues, inconsistencies, naming problems]

### Gaps
- [Missing capabilities, ranked by expected impact]

### Redundancies
- [Tools that overlap or could be merged]

### Ideas
- [Concrete suggestions for improvement, with rough effort estimates]
```

## Rules

- **Draw the pipeline first.** Understanding the flow makes individual tool assessments meaningful.
- **Read the code, don't guess.** Open every tool file and read the actual implementations. Tool names and docstrings don't tell the full story.
- **Ground in the current version.** When running devtools commands, they auto-scope to the current AGENT_VERSION. Unfiltered aggregates mix old bugs with current state.
- **Think from the agent's perspective.** The agent sees a flat list of tools. Does the naming and organization help it choose the right tool?
- **Propose, don't implement.** This is a review, not a refactoring session. Present findings and let the user decide what to act on.
- **Use AskUserQuestion** for any decisions or prioritization that need user input.
