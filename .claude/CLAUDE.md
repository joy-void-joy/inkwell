<!-- Generated from inkwell.devtools.harness.content.guidance by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. Deliberately rendered as .claude/CLAUDE.md under Claude Code, AGENTS.md under Codex. -->

# Inkwell repository guidance

**Inkwell** is an AI writing agent that turns conversations into polished, published articles. It takes a Claude conversation (or other source material), extracts a structured plan, researches every claim, writes each section in the author's voice, and produces a reviewed, fact-checked draft in a Google Doc.

Built on Python 3.13+ and the lup framework, which it takes as a git dependency rather than a copy. Keep domain code — anything about *writing* — in `src/inkwell/`, and send anything a second project on lup would want upstream to the library instead.

### Writing Pipeline

1. **Extract** — Parse Claude share links into structured conversation markdown
2. **Plan** — Extract article outline, research questions, preservable quotes, voice notes
3. **Research** — Deep research with web search, arXiv, prediction markets, economic data
4. **Write** — Parallel section writers sharing a glossary, each in their own Google Doc tab
5. **Merge** — A voice-safe pass assembles the parallel sections into one draft
6. **Review** — Parallel reviewers (narrative, fact-check, style) leave Google Doc comments
7. **Rewrite** — Final pass incorporating all reviewer + author feedback

Stage prompts and tool lists are declared in `agent/stages.py` and executed by `agent/pipeline.py`.

### Google Docs as Live Surface

The agent writes into a Google Doc the author follows in real time:

- **Tabs** for parallel section writing, so writers never conflict
- **Comments** for async Q&A — the agent asks, the author replies whenever
- **Overview tab** as a progress dashboard

The author can comment at any time; the agent picks feedback up at checkpoints. Treat the document as a surface someone is watching, not as a buffer flushed at the end.

### Naming

- **Claude** is the meta-agent working on this codebase — running commands, editing files, managing the development workflow.
- **Lup** is the framework inkwell is built on, and the name of the agent inside the code being improved. It stays `lup` everywhere the framework's own vocabulary appears — `lup_tool`, `LupMcpTool`, `lup-devtools`, and every skill spelled /lup:<skill> — because that is the framework's identity rather than a project-specific term. Only inkwell's own package is named for inkwell.

---

## Principles

### The Bitter Lesson

The single most important principle for improving this agent: **give it more tools and capabilities, not more rules.**

| Do This | Not This |
| --- | --- |
| Add tools that provide data | Add prompt rules that constrain behavior |
| Apply general principles | Apply specific pattern patches |
| Communicate principles and the _why_ | Prescribe rigid mechanical procedures |
| Provide state/context via tools | Use f-string prompt engineering |
| Reach for the strongest model and the largest thinking budget | Compensate for weak reasoning with complex prompts |
| See what went wrong from first principles | Make small edits to patch one mistake |
| Create subagents for specialized work | Build complex pipelines in the main agent |

**Tools are the primary scaffold.** When the agent struggles, the answer is almost always a missing tool — not a missing prompt paragraph.

**The test:** Does this change add a capability, or just a rule? Would it still help if the domain changed completely? If not, it is over-fitted.

### Tool Design Philosophy

Tools outlast any particular prompt revision, and they compose — each new tool multiplies the agent's options rather than constraining them.

**Prompts rot; tools don't.** If the prompt lists tool names, every addition or rename means updating two places that can drift apart. Let the agent discover tools through their descriptions.

**The tool description is the contract.** A good description answers what the tool does in concrete behavioral terms, when the agent should reach for it, and why it exists — what gap it fills. Compare `"Search the web for information"` against `"Search the web using keyword queries. Use this when the agent needs current information not available in local data, or when verifying claims against external sources. Returns a list of {title, url, snippet} results ordered by relevance."`

---

## Architecture

`docs/inkwell.md` carries the directory tree, what each pipeline stage is for, and the test principles this repository holds itself to. The pages about the framework's own machinery — the harness, the permission lattice, the resolver, the code conventions in full — are published beside it under `docs/`.

### lup (library) vs inkwell (application) Boundary

Lup is a **git dependency**, not a copy in this tree. That makes the boundary a hard one: there is no `packages/lup/` to edit here, so library changes are made in the lup repository and arrive through a dependency bump.

- **Placement test:** would a second project built on lup want this? Then it belongs in lup, not in `src/inkwell/`. Does it import from `inkwell`? Then it belongs here.
- Library code is configured through function arguments — callbacks, config objects, path overrides — never by editing its source.
- **No imports from `inkwell`** in library code. The dependency arrow points one way.
- If logic already exists in lup, import it rather than copying it.

---

## Getting Started

```bash
uv sync                         # Install dependencies
uv add <package-name>           # Add one (never edit pyproject.toml directly)
uv run ruff format . && uv run ruff check . && uv run pyright
uv run pytest

inkwell write "https://claude.ai/share/abc123"
inkwell write "https://claude.ai/share/abc123" paper.pdf -f twitter
inkwell run "write a blog post about X"
inkwell style add ~/writing/my-essay.md   # Voice-matching corpus
inkwell --help
```

**Tests:** `tests/unit/` mocks external APIs; `tests/integration/` needs API keys and is marked `@pytest.mark.integration`. `docs/inkwell.md` carries what is worth testing and what is not.

### Debugging

**Do not hypothesize — trace.** Find the actual logs, read the exact exception. Do not list "likely causes" or suggest the user check things. Open the log files, grep for the error, read the traceback, report what actually happened. If the logs lack the information, say exactly what logging to add and where. Use /lup:debug to trace an error through the logs automatically.

### Feedback Loop Scripts

```bash
uv run lup-devtools feedback collect --all-time
uv run lup-devtools feedback status
uv run lup-devtools trace list
uv run lup-devtools trace show <session_id>
```

## Plan at Agent Speed

You are an AI agent. Every instinct you have about how long software takes — sprints, milestones, "this will take weeks" — was learned from human teams, whose implementation time is scarce and expensive. Yours is not: what you would estimate as several months of work completes in an afternoon, and a "multi-day implementation" lands in about three hours. Your duration estimates are not cautious; they are wrong by orders of magnitude, and every practice built on them inverts:

- **Never scope, defer, or reject work from a predicted duration.** Scope by content — what changes, what it touches, how it is verified. If a calendar figure appears in your plan, it is noise from someone else's constraints: delete it and re-derive the plan.
- **The POC is superstition at your speed.** Prototype-first exists to keep unvalidated ideas from consuming scarce human effort; for you the complete alpha-beta-v1 costs what the throwaway was supposed to cost. Build the real implementation immediately and validate on it — let review cut scope afterward rather than pre-shrinking the attempt.
- **Catch the reflex in the act.** "Let's start with a simple version", "too ambitious for this pass", "phase 2 can add the rest" — that is a human-scarcity practice firing on constraints you do not have. When you notice it, stop and ask what is actually expensive here besides the imagined schedule.

**README.md is human-owned.** The root `README.md` is deliberately human-written, and the edit policy surfaces every change to it as Ask — as it does for any file declared under `human_owned_files` in the harness hook catalog. Never edit a human-owned file yourself — propose the exact change as a question and let the user apply or approve it.

## Agent Vocabulary

Two kinds of delegated agents look alike and must not be conflated:

- A **native subagent** ("subagent" for short) is dispatched by the harness: its delegation tool hands a focused task to a named role defined upfront, inside the main agent's session — shared trace, shared metrics.
- A **nested agent** (also called a *tool-subagent*) runs inside a tool call: the handler opens one independent session via `query()` and folds the result into the tool's response. The harness never sees it — to the calling agent it is just a tool.

Guidance that says "subagent" unqualified means the native kind. `docs/orchestration.md` carries the full delegation catalog — subagent, nested, background, deferred tool schemas — and when to reach for each. `docs/patterns.md` carries the recurring *code* shapes: declaration-plus-renderer, closed-by-construction, the typed-matcher router, and the engine-versus-surface split.

## Development Workflow

### Git Workflow

Work in a **git worktree**, not a branch switched in place, and never commit _code_ directly to `dev`. Create one with `uv run lup-devtools dev worktree create feat-name` — it lands as a sibling under `tree/`, never nested inside another checkout — and then `EnterWorktree(path=<the path it prints>)`, returning afterwards with `ExitWorktree(action="keep")`, because creating a worktree does not move the session, and edits left in the old checkout never reach the branch.

`dev` integrates and `main` carries what has landed; feature branches target `dev`, and `dev` reaches `main` through a reviewed PR. Data commits (`data(outputs):`) are the one exception that may land on `dev` directly — generated outputs need no review.

### Merge Conflict Resolution

**Never silently drop code during conflict resolution.** The bias is toward inclusion — keeping both sides is always safer than losing features. A rename on one side must not swallow an addition on the other.

Before completing any merge, **audit for deletions**: compare the result against both parents and verify that every removed function, parameter, or command was intentionally removed, not lost as a side effect of choosing one conflict side.

Use `/lup:merge` (with no argument) for guided conflict resolution. See the command for the full decision tree.

**Generated artifacts are regenerated, never hand-merged.** Take either side of the conflict, regenerate, and let the drift check confirm it settled.

### Commit Guidelines

- **Commit before responding** — Don't accumulate changes across responses
- **Commit early, commit often** — Frequent commits provide checkpoints
- **Keep commits atomic** — If you need "and" in your message, it should be two commits
- **History will be rebased** — Don't worry about perfect messages during development
- **Meaningful final commits** — After rebasing, each commit should tell what changed and why

**Format:** `type(scope): description`

| Type | Use |
| --- | --- |
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Neither fixes a bug nor adds a feature |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `chore` | Maintenance — dependencies, build config |
| `meta` | The harness declaration and what it generates |
| `data` | Generated data and outputs |

### Editing Style

**Prefer small, atomic edits.** The edit hook auto-allows a change block of at most three "real" changed lines. `docs/permissions.md` carries what counts as real, and which gates stay explicit approvals in every mode.

- Split large changes into multiple small edits (<=3 real lines per Edit call)
- Separate concerns — imports in one edit, logic in another
- Use `rename_symbol` for identifier renames instead of `Edit` with `replace_all`

---

## Code Conventions

### Primary Libraries

Build on claude-agent-sdk and pydantic, with pydantic-settings for configuration rather than dotenv; `docs/conventions.md` names each library and what it is for.

### Model Selection

Default to the **strongest** tier for the main agent, every subagent, reviewer, and background agent. This runs on a subscription where the best model is the point: reach for a **balanced** tier only when latency or cost provably dominates and quality is non-critical, and for the **fast** tier almost never. A role that genuinely warrants a cheaper model declares that tier explicitly with a reason; otherwise it inherits the strongest default. Agent declarations state the tier, not a model id — each runtime spells the tier in its own lineup.

### Type Safety

- **Never silently swallow exceptions** — no `except ...: pass`, no `contextlib.suppress`; log with `logger.exception()`, handle meaningfully, or re-raise. Catch-all `except Exception` is fine at boundaries (task loops, subagent delegation) that do so; bare `except:` and `except BaseException` are never fine
- **Every function must specify input and output types**
- **Never use `Any`, `dict[str, Any]`, or `dict[str, object]`** — Use `TypedDict` for dict-like data, `BaseModel` for validated models, or specific types
  - `docs/conventions.md` maps each origin of dict-shaped data to its typed stand-in, and lists the SDK types to prefer
- **Python 3.12+ generics**: `class A[T]`, not `Generic[T]`
- Use `TypedDict` and Pydantic models for structured data
- Never manually parse agent output — use structured outputs via Pydantic
- **Never use `# type: ignore`** — Ask the user how to properly fix type errors
- **`# lup: ignore` escape hatch** — when `Any` or another anti-pattern is genuinely needed at an untyped boundary, an inline ignore requests user approval instead of silencing the check; prefer the typed `# lup: ignore[rule-id]` over the bare form (`docs/contributing.md`), and `docs/rules.md` indexes every rule id a denial can cite
- **Use Pydantic BaseModel instead of dataclasses**
- **Use `match`/`case` instead of `if`/`elif` chains** for dispatching on values or ranges
- **Never dispatch on the type of our own models** — no `isinstance` over a union we declare, no `case ClassName()` arms, no `assert_never` net. The union's base declares the operation and each subtype answers or declines it, so a new variant is one class instead of an edit to every walk that would have to notice it, and a filter cannot go stale by omission. Narrowing untyped data at a boundary — a vendor payload, a `JsonValue` — is the different case where `isinstance` is right, because those alternatives are not ours to give a method to. The `own-model-dispatch` rule enforces exactly this line: it fires only on classes we define that inherit `BaseModel`
- **Compiling is stronger than emitting** — build an artifact from a typed declaration and it cannot diverge; transport checked source and a checker can only warn once it already has. When tempted to add a check that two things still match, ask whether one can be derived from the other instead (`docs/patterns.md`)
- **A constant should probably be an overridable default** — a canonical value (a native tool's real name, a vendor's field) is fine hardcoded; a non-canonical one (an allowlist, a ceiling, a retry count) is our judgement, so give it a default a caller can override rather than a constant they must fork to change (`docs/patterns.md`)
- **A capability ABC is an engine, not a surface** — a consumer never holds or calls one directly; it holds a concrete plain class that composes the seam and is parametrized by which implementation fills it. `ModelRouter` over `ModelMatcher` is the shape, `SessionFactory` over a `SessionOpener` the surface. The test is behaviour: a frozen value that only carries capabilities is a transparent carrier, and a seam that is only ever injected says so in its own docstring (`docs/patterns.md`)
- **Use `for`/comprehensions over `while`** — reach for structured iteration whenever the iteration space is expressible (a range, a sequence, an iterator, `enumerate`/`zip`); reserve `while` for genuinely unbounded, condition-driven loops

### Tool Input Schemas

Define tool inputs as BaseModel classes with `Field(description=...)`, and take both the `@tool` schema and the validation from that model. `docs/conventions.md` puts each form beside the raw dict it replaces.

### Error Handling

**MCP tools:** Return `{"content": [...], "is_error": True}` for recoverable errors. Log with `logger.exception()`. Include actionable messages.

**Agent code:** Raise exceptions for unrecoverable errors. Use `with_retry` for transient failures. Validate inputs early with Pydantic.

**Never silently swallow errors** — handle them meaningfully or let them propagate.

### Structured Data, Not Strings

If you're reaching for `re`, `.replace()`, `.split()`, or string slicing to process structured data, something is wrong. `docs/conventions.md` names the parser to reach for, per format.

`import re` is a code smell — look for the structured API first.

### Standard Libraries

Use existing Python libraries from PyPI before writing raw HTTP requests. Don't rebuild the wheel.

### Code as Documentation

The codebase should read as a **monolithic source of truth** — understandable without knowledge of its history.

**The test:** "Would this comment exist if the code had always been written this way?" If no — don't add it.

- Never reference what code used to do or explain modifications you made
- Never use "now", "new", "updated", "fixed", or "changed" in comments
- Use commit messages for change history, not code comments

### Inline `# lup:` Notes

A `# lup:` (or `// lup:`) comment is **actionable review feedback** left in the code for the agent to address. Four flavors, and only the removal rules differ:

| Marker | Removing it |
|---|---|
| `# lup: <text>` — open feedback | **denied**; resolve it into a claim instead |
| `# lup: solved: <text>` — a claim you addressed it | **denied**; only the verify-solved review pass retires one |
| `# lup: defer: <text>` — parked work (§ Deferred Work) | **denied** while parked |
| `# lup: ignore[<rule>]` — an anti-pattern hatch (§ Type Safety), not feedback | fine once the violation is gone |

Resolve open feedback by fixing what it points at, or, for a question, by answering it definitively in the code, the docs, or a recorded user decision. Then rewrite the marker as **`# lup: solved: <the note's original words>`**, text unchanged, so the claim sits beside what it claims to fix and can be checked against what was asked. `docs/contributing.md` carries the full lifecycle (use /lup:resolve`).

### Deferred Work

**Never create tracking files.** A `TODO.md`, backlog, or roadmap file parks a decision where no workflow will surface it again — deferral by tracking file is delegation to nobody. Deferred work lives in exactly two places: a `# lup: defer: <text>` note at the site it concerns, where `dev check` keeps it visible; or a question to the user, when whether to defer is itself the open question. Default to the bare `defer:`; a bracket states a real, externally-checkable gate, never that this code might change again. The one exception is a `tmp/` briefing, which starts a fresh session on a situation this one cannot finish, and is rewritten whole rather than appended to.

### Imports: No Barrel Files

**Never use `__init__.py` re-exports or `__all__` in internal packages.** Import directly from the module that defines the symbol.

- `from lup.mcp import lup_tool` — not `from lup import lup_tool`
- `__init__.py` files should contain only the module docstring (no imports, no `__all__`)
- Barrel files drift out of sync and hide real dependencies

**Exception:** Standalone library packages under `packages/` may use re-exports with `__all__` in their top-level `__init__.py` to declare a public API. Only the package root — not subpackages.

### Naming: No Private Prefixes

**Never use `_` prefixes** on functions, methods, classes, or constants. Nothing is private.

This holds for module-level functions, class methods, constants, and classes alike; `docs/conventions.md` shows each form beside the prefixed name it replaces.

**If a helper truly shouldn't pollute the module namespace**, nest it inside its only caller rather than marking it private.

**Avoid useless mini-wrappers.** If a function's only purpose is to call another function with no additional logic, inline it.

**Exceptions:** `_` prefix is fine for unused parameters (`_context`, `_exc_type`) — that's a linting convention, not a privacy convention.

---

## Tooling

### Package Tools

`uv` is the package manager — `uv add <package>`, never edit pyproject.toml directly. Formatting and linting are ruff, type checking is pyright; `docs/contributing.md` carries the commands that have to be green.

### lup-devtools

Development tooling is exposed as the `lup-devtools` CLI entry point, composed in `src/inkwell/devtools/main.py` from two halves: the workflow commands the library ships, and what only inkwell has beside them — its agent, its API, its trace and feedback surfaces. **Always use `lup-devtools` instead of ad-hoc commands.** Never use `uv run python -c "..."` or bare `python`/`python3` — these are denied by the Bash permission hook.

If you find yourself running the same command repeatedly, **add a command** — to the library when another project on lup would want it, to `src/inkwell/devtools/` when only inkwell would.

`tmp/` is scratch: gitignored, so nothing written there reaches a diff, a reviewer, or the human — which is why it does not execute. Match the rung to the question: to **read** code, the codeintel tools answer without running anything; to **compute** something, `lup-devtools py eval '<expression>'` auto-imports and evaluates in the sandbox; with no sandbox available, add a devtools command. `docs/contributing.md` carries the rest of the ladder, down to a heredoc behind a `# lup: escalate: <why>` marker. The argument is reviewability, not power — an agent may already edit `devtools/` and run it.

Run `uv run lup-devtools --help` for the command tree. `lup-devtools harness generate all` regenerates and reconciles the native plugin; `harness <runtime>` regenerates it and launches it. `docs/harness.md` carries the rest of the loop. Personal cache, trust, and session state are never committed.

### Lup Skills & Agents

`docs/harness.md` carries the roster of every skill and agent this plugin ships, each with the one line that describes it. Both lists are rendered from typed declarations: the ones about agent work are the library's, and anything about *writing* is declared in `src/inkwell/devtools/harness/content/catalog.py`. Change the catalog that owns the subject, then regenerate.

### Permission Hooks

Permissions come from the canonical semantic policies in `lup.policy` and the application-owned `HookSet` in `devtools/harness/catalog.py`. Harness generation compiles one hermetic dispatcher and runtime for the native plugin. Never edit generated dispatcher or runtime files.

Every shell command, URL scope, and edit in a batch is classified. Segments join deny > ask > defer > allow, and malformed input fails conservatively. `docs/permissions.md` carries the full lattice — shell vocabulary, `$(...)` recursion, write targets, fetch scopes, and edit gates. You rarely need to read it first: a denial names what tripped and how to recover.

**Two markers change a decision, so keep them in mind before you are stopped:**

- `# lup: escalate: <why>` as the leading line of a shell command promotes a classified deny or ask into an approval question carrying that reason.
- `# lup: ignore[<rule-id>]` on the offending line suppresses exactly that anti-pattern, and no other.

Use /lup:hooks to change the canonical policy inputs, regenerate, and run the shared fixture suite. `settings.json` holds only native settings outside this semantic policy boundary.

### Code Intelligence

The `codeintel` tool group answers questions about code by *resolving* it, through a language server. **Prefer them over grep for anything about a name.** `docs/conventions.md` lists what each tool answers.

**Always prefer `rename_symbol` over `Edit` with `replace_all`**, which cannot tell one scope from another; apply the edits it reports yourself.

`grep` through `Bash` is still right for what is genuinely characters: a string literal, a comment, a non-Python file.

---

## Configuration

`.env` holds defaults; `.env.local` holds secrets, is gitignored, and overrides them. Configuration is loaded through pydantic-settings in `src/inkwell/agent/config.py`, which is the only module that reads the environment — Google OAuth, research API keys, and the model and budget overrides.

Harness settings changes stay **project-level**, in the tree the harness owns (.claude/settings.json), never user-level.

---

## Process & Communication

### Asking Questions

**Always surface a question as a question**, through whatever structured question facility the harness gives you, rather than as narration the user has to notice. This applies to clarifying requirements, offering choices, confirming destructive actions, proposing changes, and any situation needing user input.

Even for open-ended questions, attach concrete options plus a free-form one. Structured answers are what downstream notification parsing reads.

**When proposing changes:** Propose (don't assume), show relevant current state, explain rationale, offer alternatives.

**When in doubt, ask.**

### Slash Commands & Skills

**After every command invocation**, reflect on how it was actually used vs. documented:

1. Compare intent vs usage
2. Notice patterns — user corrections signal the command should evolve
3. Proactively propose updates, as a question the user answers

**Evolution signals:** User provides external docs, corrects your approach, asks for something the command should cover, or ignores sections.

### External Resources

When a question is about the harness you are running under, its agent SDK, or its model API, read that runtime's own documentation rather than answering from memory:

1. Delegate to the documentation subagent your harness ships, where it has one.
2. Fetch the vendor's documentation directly — the Claude Code and Agent SDK documentation at https://docs.claude.com/ and https://code.claude.com/. The fetch scopes the permission policy admits are declared in `harness/catalog.py`.

When the user provides documentation links, incorporate that knowledge into the guidance source or the relevant skill declaration.

---

## Self-Improvement Loop

`docs/self-improvement.md` carries the full loop: how to diagnose a failure through the pipeline, the three levels of analysis, what to track per session, and the anti-patterns to avoid. Read it when running the feedback-loop, review, or meta skills — each of them works from it.

**When analyzing failures:** Ask "what general principle would have prevented this?" not "what specific rule would catch this case?" The fix is almost never a prompt line about a specific decision. Instead: does the agent have enough context? The right tools? A strong enough model?

When the principle points to a workflow failure, fix the workflow at the exact juncture where the failure enters — don't add a warning about it. A step named "Classify each commit" invites whole-commit thinking regardless of how many times the text says "decompose." Renaming the step to "Extract portable pieces" and separating reading from judging makes the failure structurally impossible. Warnings coexist peacefully with the workflows they warn against; structural changes don't.

The durable fix is a capability, not a rule: trace the failure to the missing
input or the workflow step where the wrong decision entered, and change that.
A prompt rule coexists peacefully with the failure it warns about.
