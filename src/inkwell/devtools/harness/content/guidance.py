"""Canonical repository guidance for inkwell.

The portable conventions are composed from ``lup.devtools.harness.content
.conventions`` rather than restated here, so this document holds only what is
true of *inkwell*: what it writes, the pipeline that writes it, the Google Doc
it writes into, and the boundary between the writing application and the
framework it takes as a dependency.

What a reader needs at a particular moment rather than on every turn lives in
a generated page under ``docs/`` with a file-path pointer from here — the
always-loaded document is held to a byte budget, and reference material is
what that budget is for.
"""

import lup.devtools.harness.content.conventions as conventions
import lup.harness.models as models
from lup.codescan.common import RuleSelection


def guidance_parts(selection: RuleSelection) -> list[models.PromptPart]:
    """Inkwell's guidance, naming only the rules it still enforces."""
    return [
        models.TextPart(
            text=r"""# Inkwell repository guidance

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

### A Work of Many Parts

That pipeline writes one piece. A book is a tree of them, and `manuscript/` is the build loop over that tree — read a work into parts, ask which are out of date and why, run those, ask again until a pass finds nothing. The revisable unit is a subsection, three levels down, not a chapter.

- **A part run is one ordinary run.** The part's own text is the material, under `source`, and nothing here re-implements a stage. Composed at the outside deliberately, which is what keeps a book-length feature out of `agent/pipeline.py`.
- **The part is written, not anchored.** Routed as the piece a run *replaces*, the standing text becomes the plan and its section order survives the rewrite. Routed as material, the run answers what the part has to establish — and what the standing text carried is protected by the inheritance pass, whose audit is subtracted from what the two texts actually hold rather than believed.
- **The loop is the single writer of state.** Parts run concurrently with no lock because each writes prose into a span that cannot overlap another's and hands back what it did.
- **Propagation keys on what changed, not who changed it**, which is why a pass settles rather than cascading.
- **The work declares how its parts are written**, once at import, and every run inherits it — the format, whether one writer drafts a part or one drafts each planned section, and which stages a part run skips. A part asked to infer any of these from the one subsection it sees answers differently on the next part. Drafting defaults to a single writer: the book has already done the splitting, and splitting a subsection again buys nine writers and a merge to produce what one writer produces in one pass.
- **Parked is not failed**, and both are sticky on purpose: a run that cannot settle something asks and its part waits at no cost, while a failed part keeps what it said. Both need somebody to open the door.

### The Research Corpus

`corpus/` holds documents enumerated from declared sources, tagged against a vocabulary and optionally embedded. The first three stages run in one order — fetch, judge, then embed, because embedding reads what judging wrote. The plan stage is *handed* a briefing of what the corpus already holds on the topic rather than left to search for it: what the corpus has and the source material does not is the strongest research question there is, and no question derived from the source could reach it.

`distil` is the fourth, and it runs lazily rather than in that order: a document is read whole the first time a filter reaches it, and what it establishes is cached under its `content_sha256` for good. The judging cannot carry it — the judge reads six pages on purpose, and a finding is on page 34. `manuscript research` is what puts the two together: it distils what a work's filter reaches, places the findings on the parts they bear on by reading the tree's titles, and turns each new placement into a `finding` change fact, so the ordinary sweep dirties those parts and the loop names the paper. Placing moves nothing until somebody says so; the reading is priced first with `--dry-run`.

`docs/inkwell.md` carries both of these in full — the store layout, the sweep ladder, and every standing a part can hold.

### Google Docs as Live Surface

The agent writes into a Google Doc the author follows in real time:

- **Tabs** for parallel section writing, so writers never conflict
- **Comments** for async Q&A — the agent asks, the author replies whenever
- **Overview tab** as a progress dashboard

The author can comment at any time; the agent picks feedback up at checkpoints. Treat the document as a surface someone is watching, not as a buffer flushed at the end.

### Naming

- **Claude** is the meta-agent working on this codebase — running commands, editing files, managing the development workflow.
- **Lup** is the framework inkwell is built on, and the name of the agent inside the code being improved. It stays `lup` everywhere the framework's own vocabulary appears — `lup_tool`, `LupMcpTool`, `lup-devtools`, and every skill spelled """
        ),
        models.SkillPattern(plugin="lup", placeholder="<skill>"),
        models.TextPart(
            text=r""" — because that is the framework's identity rather than a project-specific term. Only inkwell's own package is named for inkwell.

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

uv run inkwell-web                        # Sessions and works, in the browser
uv run lup-devtools manuscript import <chapters> --work atlas
uv run lup-devtools manuscript status atlas --dirty
uv run lup-devtools manuscript run atlas --dry-run --limit 1
uv run lup-devtools corpus pipeline       # Fetch, judge, then embed
```

**Tests:** `tests/unit/` mocks external APIs; `tests/integration/` needs API keys and is marked `@pytest.mark.integration`. `docs/inkwell.md` carries what is worth testing and what is not.

### Debugging

**Do not hypothesize — trace.** Find the actual logs, read the exact exception. Do not list "likely causes" or suggest the user check things. Open the log files, grep for the error, read the traceback, report what actually happened. If the logs lack the information, say exactly what logging to add and where. Use """
        ),
        models.SkillInvocation(plugin="lup", skill="debug"),
        models.TextPart(
            text=r""" to trace an error through the logs automatically.

### Feedback Loop Scripts

```bash
uv run lup-devtools feedback collect --all-time
uv run lup-devtools feedback status
uv run lup-devtools trace list
uv run lup-devtools trace show <session_id>
```

"""
        ),
        *conventions.PLAN_AT_AGENT_SPEED,
        *conventions.AGENT_VOCABULARY,
        *conventions.THE_GATES,
        models.TextPart(
            text=r"""## Development Workflow

### Git Workflow

Work in a **git worktree**, not a branch switched in place, and never commit _code_ directly to `dev`. Create one with `uv run lup-devtools dev worktree create feat-name` — it lands as a sibling under `tree/`, never nested inside another checkout — and then """
        ),
        models.RelocateSession(path="the path it prints"),
        models.TextPart(
            text=r""", because creating a worktree does not move the session, and edits left in the old checkout never reach the branch.

`dev` integrates and `main` carries what has landed; feature branches target `dev`, and `dev` reaches `main` through a reviewed PR. Data commits (`data(outputs):`) are the one exception that may land on `dev` directly — generated outputs need no review.

"""
        ),
        *conventions.MERGE_CONFLICT_RESOLUTION,
        models.TextPart(
            text=r"""**Generated artifacts are regenerated, never hand-merged.** Take either side of the conflict, regenerate, and let the drift check confirm it settled.

"""
        ),
        *conventions.COMMIT_GUIDELINES,
        models.TextPart(
            text=r"""---

## Code Conventions

Build on claude-agent-sdk and pydantic, with pydantic-settings for configuration rather than dotenv; `docs/conventions.md` names each library and what it is for, and puts each typed form beside the raw dict it replaces — including tool inputs, which are BaseModel classes with `Field(description=...)` that give both the `@tool` schema and the validation.

Use existing Python libraries from PyPI before writing raw HTTP requests. Don't rebuild the wheel.

### Model Selection

Default to the **strongest** tier for the main agent, every subagent, reviewer, and background agent. This runs on a subscription where the best model is the point: reach for a **balanced** tier only when latency or cost provably dominates and quality is non-critical, and for the **fast** tier almost never. A role that genuinely warrants a cheaper model declares that tier explicitly with a reason; otherwise it inherits the strongest default. Agent declarations state the tier, not a model id — each runtime spells the tier in its own lineup.

"""
        ),
        models.TextPart(
            text=r"""### Error Handling

**MCP tools:** Return `{"content": [...], "is_error": True}` for recoverable errors. Log with `logger.exception()`. Include actionable messages.

**Agent code:** Raise exceptions for unrecoverable errors. Use `with_retry` for transient failures. Validate inputs early with Pydantic.

**Never silently swallow errors** — handle them meaningfully or let them propagate.

**Never silently truncate content** — the container grows to fit what it holds, not the reverse. Cut only where a document format or a function contract imposes a hard limit, never for printing space, log volume, or ease of reading, and where a cut is forced save the full copy and point at it from what survives. A cut artifact looks exactly like a complete one, which is why `[:200]` on something an author wrote loses the rest with nothing said.

"""
        ),
        *conventions.design_principles(selection),
        *conventions.SANCTIONED_EXCEPTIONS,
        models.TextPart(
            text=r"""### Inline `# lup:` Notes

A `# lup:` (or `// lup:`) comment is **actionable review feedback** left in the code for the agent to address. A note that runs to several lines carries the marker on its **first line only**, continuing with bare `#` comments — every `# lup:` line starts a new note, so repeating the marker turns one concern into a note per line, and the resolver then plans each fragment separately. Four flavors, and only the removal rules differ:

| Marker | Removing it |
|---|---|
| `# lup: <text>` — open feedback | **denied**; resolve it into a claim instead |
| `# lup: solved: <text>` — a claim you addressed it | **denied**; only the verify-solved review pass retires one |
| `# lup: defer: <text>` — parked work (§ Deferred Work) | **denied** while parked |
| `# lup: ignore[<rule>]` — the rule-checker hatch (§ The Gates You Will Meet), not feedback | fine once the violation is gone |

Resolve open feedback by fixing what it points at, or, for a question, by answering it definitively in the code, the docs, or a recorded user decision. Then rewrite the marker as **`# lup: solved: <the note's original words>`**, text unchanged, so the claim sits beside what it claims to fix and can be checked against what was asked. `docs/contributing.md` carries the full lifecycle (use """
        ),
        models.SkillInvocation(plugin="lup", skill="resolve"),
        models.TextPart(
            text=r"""`).

### Deferred Work

**Never create tracking files.** A `TODO.md`, backlog, or roadmap file parks a decision where no workflow will surface it again — deferral by tracking file is delegation to nobody. Deferred work lives in exactly two places: a `# lup: defer: <text>` note at the site it concerns, where `dev check` keeps it visible; or a question to the user, when whether to defer is itself the open question. Default to the bare `defer:`; a bracket states a real, externally-checkable gate, never that this code might change again. The one exception is a `tmp/` briefing, which starts a fresh session on a situation this one cannot finish, and is rewritten whole rather than appended to.

"""
        ),
        models.TextPart(
            text=r"""---

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

Change the policy those gates enforce with """
        ),
        models.SkillInvocation(plugin="lup", skill="hooks"),
        models.TextPart(
            text=r""", which edits the canonical policy inputs, regenerates the plugin, and runs the shared fixture suite. `settings.json` holds only native settings outside this semantic policy boundary.

### Code Intelligence

The `codeintel` tool group answers questions about code by *resolving* it, through a language server. **Prefer them over grep for anything about a name.** `docs/conventions.md` lists what each tool answers.

**Always prefer `rename_symbol` over `Edit` with `replace_all`**, which cannot tell one scope from another; apply the edits it reports yourself.

`grep` through `Bash` is still right for what is genuinely characters: a string literal, a comment, a non-Python file.

---

## Configuration

`.env` holds defaults; `.env.local` holds secrets, is gitignored, and overrides them. Configuration is loaded through pydantic-settings in `src/inkwell/agent/config.py`, which is the only module that reads the environment — Google OAuth, research API keys, and the model and budget overrides.

Harness settings changes stay **project-level**, in the tree the harness owns ("""
        ),
        models.NativePath(location="project_settings"),
        models.TextPart(
            text=r"""), never user-level.

---

## Process & Communication

### Asking Questions

**Always surface a question as a question**, through whatever structured question facility the harness gives you, rather than as narration the user has to notice. This applies to clarifying requirements, offering choices, confirming destructive actions, proposing changes, and any situation needing user input.

Even for open-ended questions, attach concrete options plus a free-form one. Structured answers are what downstream notification parsing reads.

**When proposing changes:** Propose (don't assume), show relevant current state, explain rationale, offer alternatives.

**When in doubt, ask.**

### Reporting a Finding

Report at the level the question was asked. A finding is the one thing an investigation establishes, and the steps that led to it are not the finding — reconstructing the whole progression buries the answer rather than supporting it.

**Separate the subject's fault from the environment's.** Most steps in a hard diagnosis turn out to be local misconfiguration. Saying so in one clause is worth more than a section each, and a report that does not separate them misattributes cause: an issue titled after the error message rather than the defect is simply wrong, however carefully the evidence was gathered.

Detail earns its place when the reader needs it to act, and nowhere else.

### Slash Commands & Skills

**After every command invocation**, reflect on how it was actually used vs. documented:

1. Compare intent vs usage
2. Notice patterns — user corrections signal the command should evolve
3. Proactively propose updates, as a question the user answers

**Evolution signals:** User provides external docs, corrects your approach, asks for something the command should cover, or ignores sections.

### External Resources

When a question is about the harness you are running under, its agent SDK, or its model API, read that runtime's own documentation rather than answering from memory:

1. Delegate to the documentation subagent your harness ships, where it has one.
2. Fetch the vendor's documentation directly — """
        ),
        models.RuntimeDocs(),
        models.TextPart(
            text=r""". The fetch scopes the permission policy admits are declared in `harness/catalog.py`.

When the user provides documentation links, incorporate that knowledge into the guidance source or the relevant skill declaration.

---

## Self-Improvement Loop

`docs/self-improvement.md` carries the full loop: how to diagnose a failure through the pipeline, the three levels of analysis, what to track per session, and the anti-patterns to avoid. Read it when running the feedback-loop, review, or meta skills — each of them works from it.

"""
        ),
        *conventions.FAILURE_ANALYSIS,
        models.TextPart(
            text=r"""The durable fix is a capability, not a rule: trace the failure to the missing
input or the workflow step where the wrong decision entered, and change that.
A prompt rule coexists peacefully with the failure it warns about.
"""
        ),
    ]


def document(selection: RuleSelection | None = None) -> models.PromptDocument:
    """The guidance as one document, built against the project's selection.

    Taking the selection rather than reading one keeps the catalog free to
    import this module: the catalog owns the declaration and hands it down,
    so nothing here reaches back up for it.
    """
    return models.PromptDocument(
        source=__name__, parts=guidance_parts(selection or RuleSelection())
    )
