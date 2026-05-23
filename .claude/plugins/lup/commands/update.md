---
allowed-tools: Bash(git:*, uv run lup-devtools:*), Read, Grep, Glob, Edit, Write, AskUserQuestion, Skill(lup:commit)
description: Review upstream template commits and apply improvements
argument-hint: [focus area]
---

# Update from Upstream

Review commits from tracked downstream repositories since the last sync. Generalize domain-specific patterns back into the template as domain-neutral scaffolding, then apply selected improvements.

**This repo is both a template and a scaffold.** Downstream repos customize the template (prompts, tools, models) but inherit the scaffold (agents, commands, hooks, workflows). When reviewing downstream changes, ask: "Did this pattern emerge from real use?" If yes, it probably belongs in the template — generalized, with domain-specific details removed.

**Optional focus argument:** When a focus area is provided (e.g., `/lup:update hooks`, `/lup:update lib/cache`), only review and port commits that touch the specified area. **Do not mark as synced** — the sync pointer stays unchanged so a future `/lup:update` (without args) still reviews all commits from the same checkpoint.

## Setup

If `downstream.json` does not exist, help the user set it up.

**Self-referencing repos:** When the current repo IS the upstream (e.g., the lup template itself), set `"ignore": true` in `downstream.json.local` to skip it during updates. The committed `downstream.json` still ships the URL so downstream users can sync from it.

```bash
# Set a local path for the lup template repo
uv run lup-devtools sync setup lup /path/to/lup-template.git/tree/main

# Or mark it as already synced at current HEAD (skip old history)
uv run lup-devtools sync setup lup /path/to/lup-template.git/tree/main --synced
```

Ask the user for the path to their lup template repo if not already tracked.

## Process

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before applying upstream changes.

### 2. Check for new commits

```bash
uv run lup-devtools sync list
```

If no projects have new commits, report that everything is up to date and stop.

### 3. Read all diffs and build inventory

For each project with new commits:

```bash
uv run lup-devtools sync log <project>
```

Skip **data-only** commits (`data(outputs):`, `data(scores):`). For every other commit, read the **complete** diff — never truncate with `head` or skim large outputs:

```bash
uv run lup-devtools sync diff <project> <sha>
```

After reading each diff, produce an **inventory**: list every new function, class, CLI command, model, and pattern added. The inventory is purely descriptive — what was added, not whether it's useful. This prevents whole-commit dismissal: you can't skip what you've already enumerated.

Do not classify during this step. Cross-commit patterns only become visible after reading all diffs.

**If a focus area was provided:** Also skip commits whose messages clearly don't relate to the focus area. But when in doubt, keep them — the diff might touch relevant code.

### 4. Classify inventory

For each item in the inventory, ask: **"What would this look like with a generic data source?"** If you can describe a generic version, it's portable. If the item IS the domain data (model fields, API-specific calls, scoring formulas), it's domain-specific.

**Do not confuse the data a tool operates on with the tool itself.** Visualization commands, CLI watch modes, analysis pipelines, and formatting utilities are infrastructure — portable even when they currently display domain-specific data. The data source is a parameter; the infrastructure is the portable piece.

Classify as:

- **Portable as-is**: Improvements that apply directly without modification
  - `lup` library utilities (e.g., better `print_block`, new retry patterns, caching improvements)
  - `devtools/` CLI improvements (new subcommands, better output formatting, new analysis tools)
  - Hook logic improvements (new permission patterns, better auto-allow rules)
  - Build/config improvements that generalize
  - **CLAUDE.md improvements** (coding standards, workflow tips, new guidelines)

- **Portable as scaffold**: Domain-specific implementations that represent a generalizable _pattern_. These get ported with domain details replaced by template placeholders.
  - **New agents/subagents** — A "version-reviewer" that uses Brier scores becomes a scaffold version-reviewer that uses generic outcome metrics. A "forecast reviewer" sub-agent becomes a generic "reviewer" scaffold that critiques agent output.
  - **New tools or tool patterns** — A domain-specific reflection tool becomes a scaffold for structured self-assessment tools. A tool that calls a sub-agent internally is a reusable pattern.
  - **New commands** — A "leak-investigator" for retrodiction becomes a scaffold for investigator-style commands
  - **Workflow improvements** — Offline mode for a specific API becomes a general "graceful degradation" pattern
  - **Reusable lib patterns** — A "response collector" that prints+logs SDK blocks is a general utility. A JSON pretty-printer for tool results belongs in lib.
  - **Feedback loop updates** — Version-scoped analysis, new analysis phases, better templates
  - **Agent SDK usage patterns** (hooks, session config, structured output, tool patterns)
  - **Agent core improvements** that generalize (error handling, log management, config patterns)
  - **Scoring/metrics improvements** (new columns, aggregation methods, visualization commands)

Examples of what gets missed when you classify by domain keywords instead of by function:
- A "forge image mounting" commit also adds `save_images()` — a general utility for writing clipboard image data to disk. The forge wiring is domain-specific; the utility function is portable.
- A "REPL upgrade" commit adds prompt_toolkit, clipboard paste, *and* container orchestration changes. The REPL UX is portable; the container setup is not.
- A "sandbox tools" commit adds `run_code` *and* a new error-handling pattern in the tool wrapper. The tool is domain-specific; the error-handling pattern is portable.
- A "score visualization" commit adds strip plots, trend charts, color selection, and watch mode. The scoring formula is domain-specific; the visualization pipeline is portable devtools infrastructure.

### 5. Present improvements

Present every extracted portable piece to the user via AskUserQuestion:

- The upstream commit(s) it comes from
- What the portable piece is (function, pattern, infrastructure, etc.)
- Where it maps to in the template
- Whether to apply it

Group related pieces when they form a logical unit (e.g., strip plot + trend plot + watch mode = "devtools visualization scaffold").

**Default: present.** When uncertain whether something is portable, present it with your reasoning and let the user decide. The user can always say "skip" — but you cannot un-skip something you never showed them.

### 6. Apply selected changes

For approved improvements:

1. Read the full changed files in both repos to understand context
2. Apply the changes, adapting as needed:
   - Adapt Python import paths between upstream and current project package names (`from lup.*` ↔ `from <project>.*`)
   - Framework vocabulary stays as `lup` in both directions — do not rename `lup_tool`, `LupMcpTool`, `lup-devtools`, `.lup/`, `lup-tools`, `lup-sandbox-*`, etc.
   - Keep the current project's coding conventions
3. **Wire new utilities into consumers.** When porting a library function (e.g., a helper in `packages/lup/`), don't stop at the function itself — also wire it into the devtools commands, hooks, or agents that should use it. A utility without consumers is dead code.
4. Run verification after applying:
   ```bash
   uv run lup-devtools dev check
   ```

### 7. Mark as synced

**Skip this step if a focus area was provided** — the sync pointer must stay unchanged so unreviewed commits are still visible in the next full `/lup:update`.

After a full review is complete (whether or not changes were applied):

```bash
uv run lup-devtools sync mark-synced <project>
```

### 8. Optionally commit

If changes were applied, offer to commit them:

```bash
git add <changed-files>
git commit -m "feat(lib): apply improvements from <project>"
```

## Guidelines

- **Commit-level review preserves intent** — review commits, not flat diffs, so you understand why each change was made
- **File diffs provide context** — use full file diffs alongside commit diffs to understand how changes fit into the codebase
- **Generalize, don't dismiss** — when a downstream repo adds something domain-specific, ask "what pattern does this represent?" and port the pattern as scaffold. A forecasting-specific agent becomes a domain-neutral agent scaffold.
- **Ask, don't skip** — when uncertain about a change, present it to the user with your reasoning and let them decide
- **Adapt, don't copy** — downstream code uses domain-specific naming, paths, and models. Replace these with template-appropriate equivalents (`lup` package paths, generic metrics, placeholder descriptions).
- **Re-evaluate file placement** — after generalizing, re-check: can this module be used as-is without templating or source modification? If yes, it belongs in `packages/lup/`, not `agent/`. A quick proxy: does it import from `agent/`? Downstream repos may have had domain-specific reasons for a different placement.
- **Test after applying** — always run pyright/ruff/pytest after applying changes
- **Mark synced even if nothing applied** — this advances the sync pointer so you don't re-review the same commits next time