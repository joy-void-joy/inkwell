<!-- Generated from inkwell.devtools.harness.content.docs.inkwell by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# The writing agent

Inkwell is one application built on lup, which it takes as a git dependency.
There is no `packages/lup/` in this tree: the library is resolved from its
repository, so a change to it is a change made *there* that arrives here
through a dependency bump. Everything below is inkwell's own.

## Directory structure

```
src/inkwell/
├── agent/                  # Reasoning: pipeline orchestration, prompts, tools
│   ├── client.py           # Session factory, stage policy, the query helper
│   ├── core.py             # Main orchestration
│   ├── config.py           # Settings — Google OAuth, research API keys, budgets
│   ├── models.py           # ArticlePlan, WritingOutput, ReviewFinding
│   ├── provenance.py       # Venue, evidential role, and acquisition records
│   ├── diversity.py        # Citation distribution and position-diversity checks
│   ├── prompts.py          # System prompt for the writing agent
│   ├── stages.py           # Stage prompts and tool lists, per pipeline stage
│   ├── pipeline.py         # The unified pipeline and its listener
│   ├── format_checks.py    # The rows a format declares beside its guidance
│   ├── prose.py            # A draft read as blocks and sentences
│   ├── segmenter.py        # The syntok segmenter filling the sentence seam
│   ├── session.py          # WritingSessionState, WritingContext
│   ├── tool_policy.py      # Conditional tool availability
│   ├── watcher.py          # Background agent watching for author feedback
│   └── tools/
│       ├── google_docs.py  # Create, write, comment, and tab management
│       ├── author.py       # ask_author, check_feedback
│       ├── voice.py        # Voice analysis and the style corpus
│       ├── extract.py      # Source extraction — conversations, URLs, files
│       ├── formats.py      # Output adapters — LessWrong, Twitter, blog
│       ├── latex.py        # Sandboxed LaTeX rendering
│       └── research/       # exa, arxiv, fred, markets, wikipedia, fetch, corpus
├── corpus/                 # The research corpus — material gathered before the question
│   ├── registry.py         # Every tracked source declared once: hosts, avenues, authority
│   ├── discovery.py        # Sitemap, listing, table, and sweep enumeration
│   ├── quality.py          # Declared quality rules with overridable thresholds
│   ├── storage.py          # Documents as files beside one typed JSON index per source
│   ├── fetch.py            # Reaching a document, escalating to a browser where declared
│   └── ingest.py           # A run: enumerate, fetch what is new, store, report
├── devtools/               # Development CLI, exposed as `lup-devtools`
│   ├── main.py             # Root Typer app composing the sub-apps
│   ├── harness/            # Typed harness declarations — this tree's source
│   ├── trace/              # Trace display, search, and analysis
│   ├── feedback/           # Feedback state, metrics, and session commits
│   └── version.py          # Version display, changelog, and bump
└── environment/            # I/O: how a run starts and what happens to a result
    ├── cli/                # Typer CLI and the interactive chat session
    └── web/                # Session API and the browser surface
```

Keeping `agent/` free of I/O is what makes it improvable: the self-improvement
loop reads traces and changes prompts, tools, and models, and never has to
reason about where a session came from.

## Pipeline stages

Declared in `agent/stages.py` as a prompt and a tool list each, executed by
`agent/pipeline.py`. The stage name is what a cost sink and a trace label are
keyed on, so it is the unit the feedback loop reports against.

| Stage | Produces |
| --- | --- |
| `planner` | The article outline, research questions, preservable quotes, voice notes |
| `researcher` | Answers to every research question, with sources |
| `section_writer` | One section, in its own Google Doc tab, sharing a glossary |
| `merge` | A single draft assembled from the parallel sections, voice-safe |
| `narrative_reviewer` | Comments on structure and argument |
| `fact_checker` | Comments on every checkable claim |
| `style_reviewer` | Comments on voice and prose |
| `rewriter` | The final draft, incorporating reviewer and author feedback |

Section writers run in parallel and never share a tab, which is what makes the
Google Doc safe to write into while the author is reading it.

## Output formats and the checks they declare

An `OutputFormatSpec` in `agent/stages.py` carries a format's prose guidance
and, beside it, the rows that guidance is re-read against. Both come off one
declaration: `get_format_guidance` renders the rows into what the writer reads,
and the rewrite stage measures the same rows off the finished draft, so a rule
the writer was given and a rule the draft was checked by cannot drift apart.
Adding a row to a format is a constructor call in that format's list; adding a
*kind* of row is one class in `agent/format_checks.py`.

Two tiers, one row shape. **Mechanical** rows are data — a threshold and the
guidance sentence they measure — and read the draft structurally through
`agent/prose.py`: blocks from markdown-it, sentences and tokens from the
segmenter in `agent/segmenter.py`. **Judged** rows spend a reviewer on what
counting cannot settle, and land the verdict in the same `CheckRow`, so the
rewrite stage reads one report.

`Segmenter` is the seam that decides how much a row can know about a sentence,
and syntok fills it: boundaries an abbreviation or a decimal does not fool, plus
tokens a row can match a construction against by position. syntok is here
because it is pure Python and the project floor is 3.14, which no spaCy wheel
covers; `pyproject.toml` declares spaCy as the `pos` extra to record where a
parser-backed implementation drops in. The two rows that would read a parse —
copula avoidance and participial tails — ship as declared detectors over those
tokens, matching the tells the author enumerated, and generalize the day the
seam is filled by a parser without changing.

Rows are **advisory in the `dev check` sense**: they report and never gate. A
fired row is a line in an artifact the rewriter is handed, a failure to measure
at all is logged and dropped, and the author's voice outranks every row —
where one fires against how the author actually writes, the author wins. A
format whose guidance states nothing measurable declares no rows. A format
invented for one run declares its own at runtime through
`declare_format_check`, validated against the same models Python uses.

Where two formats want opposite things from the same measurement, the row
carries the difference as data rather than the code carrying a special case:
`BoldedSummaries` states the textbook floor and the memo ceiling, and
`BoldEmphasis` takes the exemption that lets bold be navigation in a format
whose convention asks for it while staying overuse everywhere else.

## Test principles

**Test behavior, not construction.** Never test that a constructor sets
attributes — that tests Pydantic, not this code. A pure data container with no
methods, computed properties, or custom validation does not need tests.

**Every test should answer: "what could go wrong?"** If nothing can go wrong,
the test is worthless. Good tests exercise state transitions, edge cases
(empty inputs, missing files, duplicate names, boundaries), invariants that
must hold across operations, and integration points — does this read from disk
correctly, does it compose with its dependencies?

**The test for a test:** remove it. Does the remaining suite still catch real
bugs? If yes, it was dead weight.

| Write tests for | Don't write tests for |
| --- | --- |
| Computed properties that read from disk | Pydantic model construction |
| Registry CRUD with state verification | Attribute access after `__init__` |
| Error paths and graceful degradation | Default field values |
| Multi-step workflows (add → use → remove) | Constants |
| Concurrency and timing behavior | Sorted output of deterministic functions |

`tests/unit/` mocks external APIs. `tests/integration/` needs real API keys and
is marked `@pytest.mark.integration`.
