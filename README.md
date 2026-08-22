# Inkwell

An AI writing agent that turns source material into a reviewed, fact-checked draft — and keeps a
book of many parts up to date once it is written.

Inkwell takes a conversation, a paper, a web page, or an existing draft; extracts a structured plan;
researches every claim it rests on; writes each section in the author's voice; and puts the result in
a Google Doc the author can watch and comment on while it happens. It is built on
[lup](https://github.com/joy-void-joy/lup), which it takes as a git dependency — lup is the agent
framework and the development scaffolding, inkwell is everything about *writing*.

## What it does

### One piece, written end to end

```bash
inkwell write "https://claude.ai/share/abc123"
inkwell write "https://claude.ai/share/abc123" paper.pdf -f twitter
inkwell run "write a blog post about X"
```

Seven stages, declared in `agent/stages.py` and executed by `agent/pipeline.py`:

1. **Extract** — parse the source into structured markdown
2. **Plan** — an outline, the research questions, quotes worth preserving, notes on voice
3. **Research** — web search, arXiv, prediction markets, economic data, and the local corpus
4. **Write** — parallel section writers sharing a glossary, each in its own Google Doc tab
5. **Merge** — a voice-safe pass assembling the parallel sections into one draft
6. **Review** — parallel reviewers (narrative, fact-check, style) leaving Google Doc comments
7. **Rewrite** — a final pass over reviewer and author feedback

The Doc is a live surface rather than a buffer flushed at the end: tabs keep parallel writers from
conflicting, comments carry async Q&A in both directions, and an overview tab reports progress. The
author can comment at any time and the agent picks it up at the next checkpoint.

### A book, kept up to date

A pipeline run writes one piece. A textbook is a tree of them, and the unit anybody actually revises
sits three levels down — a subsection, not a chapter. `manuscript/` is a build system over that
tree:

```bash
lup-devtools manuscript import ~/textbook/docs/chapters --work atlas
lup-devtools manuscript status atlas --dirty        # what is out of date, and why
lup-devtools manuscript reaches atlas Superintelligence   # what touching it commits you to
lup-devtools manuscript run atlas --dry-run --limit 1
```

- **Importing adopts.** Every part is stamped as built from the text it already holds, so the first
  useful pass rewrites what somebody asked about rather than the whole book. `make -t`, and the same
  argument.
- **A part run is one ordinary pipeline run** whose material happens to be a span of somebody's
  book. Its prose is spliced back into place; its siblings stay byte-identical.
- **Propagation keys on what changed, not on who changed it.** Sharpen a term in one subsection and
  exactly the parts that use it go out of date — which is why a pass settles instead of cascading.
- **The work declares its format** once at import, and every run against it inherits it.
- **Parked is not failed.** A run that cannot settle something asks a question and its part waits,
  costing nothing, until somebody answers.

### A corpus gathered before the question

`corpus/` enumerates documents from declared sources — labs, evaluators, governance shops, press —
tags them against a vocabulary, and optionally embeds them:

```bash
lup-devtools corpus pipeline          # fetch, judge, then embed, in the one order that works
lup-devtools corpus status
lup-devtools corpus brief "capability forecasting"
```

The plan stage is *handed* a briefing of what the corpus already holds on the topic rather than left
to search for it. What the corpus has and the source material does not is the strongest research
question available, because no question derived from the source could reach it.

## Surfaces

```bash
uv run inkwell-web        # http://127.0.0.1:4545
inkwell --help
```

The web app carries both halves: **sessions**, where one run is started and watched live with its
stages, cost, logs, and prompts, and **works**, where a book is imported, its tree browsed by what
is outstanding, a part asked for, a parked question answered, and the loop started — after a preview
of what it would pick up, because every part is a whole pipeline run. The CLI does the same through
`inkwell` and `lup-devtools`; both surfaces call the same recording and launch paths, so a work
imported from either ends up in the same state.

## Getting started

```bash
uv sync                                   # needs uv; docker only for the sandboxing extras
cp .env .env.local                        # secrets and overrides go in .env.local, gitignored
uv run lup-devtools setup                 # Google OAuth and the research API keys
uv run pytest
```

Configuration is loaded through pydantic-settings in `agent/config.py`, the only module that reads
the environment. The corpus, the book records, and the manuscript store live beside the checkout
rather than inside a session, so every worktree reads one corpus.

## Working on it

Development runs through `lup-devtools`, composed from the workflow commands lup ships and the ones
only inkwell has beside them:

```bash
uv run lup-devtools dev check             # format, lint, types, tests, rules, drift
uv run lup-devtools dev worktree create feat-name
uv run lup-devtools trace show <session_id>
uv run lup-devtools feedback status
```

Work happens in a git worktree under `tree/`, never on a branch switched in place. Feature branches
target `dev`; `dev` reaches `main` through a reviewed PR.

Several gates enforce the repository's conventions so nobody has to hold them in memory — an
executable rule checker, a semantic permission policy over shell commands and edits, an edit budget
that auto-allows small changes and asks about large ones, and a drift check over generated trees.
Each names what it caught and how to answer it. `docs/rules.md` indexes every rule, generated from
the same registry that runs.

## Documentation

`docs/inkwell.md` is the map: the directory tree, what each pipeline stage is for, the manuscript
build loop and the corpus in full, and the test principles this repository holds itself to. Beside
it, `docs/architecture.md`, `docs/conventions.md`, `docs/patterns.md`, `docs/permissions.md`,
`docs/harness.md`, and `docs/orchestration.md` carry the framework machinery, the code shapes, the
permission lattice, the plugin, and the delegation catalog. `.claude/CLAUDE.md` is the always-loaded
guidance, generated from a typed declaration in `devtools/harness/content/`.

## License

MIT. See [LICENSE](LICENSE).
