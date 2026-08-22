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
│   ├── book.py             # Which chapter of which book, and the record it outlives
│   ├── book_links.py       # A chapter pointing at the book, resolved at the write
│   ├── glossary.py         # The shared term ledger: per run, across a book, across a work
│   ├── diversity.py        # Citation distribution and position-diversity checks
│   ├── prompts.py          # System prompt for the writing agent
│   ├── stages.py           # Stage prompts and tool lists, per pipeline stage
│   ├── pipeline.py         # The unified pipeline and its listener
│   ├── format_checks.py    # The rows a format declares beside its guidance
│   ├── voice_tells.py      # The house voice's banned vocabulary and its rows
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
│   ├── tags.py             # The declared tag vocabulary a browse filters on
│   ├── tagging.py          # Derive a source's tags, judge a document's, re-tag on edit
│   ├── storage.py          # Documents as files beside one typed JSON index per source
│   ├── retrieval.py        # Browse, narrow, read — field navigation over the index
│   ├── semantics.py        # The optional vector layer, and the seam that computes it
│   ├── fetch.py            # Reaching a document, escalating to a browser where declared
│   ├── archive.py          # Reading a gated document from a public archive's capture
│   └── ingest.py           # A run: enumerate, fetch what is new, tag, store, report
├── manuscript/             # A work of many parts, built incrementally rather than written once
│   ├── tree.py             # The tree a work is, and the key each part keeps across imports
│   ├── ingest.py           # Reading an mkdocs work: declared order, then headings below the files
│   ├── vocabulary.py       # The terms a work's own authors declared, and which parts use them
│   ├── links.py            # The parts one part points at, read out of its prose
│   ├── facts.py            # What a run read, what it changed, and the ledger between them
│   ├── state.py            # Declared standings and build stamps — dirtiness is derived, never stored
│   ├── graph.py            # The dependency pass, and adopting a work as already built
│   ├── splice.py           # Replacing one part's span, leaving its siblings byte-identical
│   ├── store.py            # A work's records, outliving every run and outside its own repository
│   ├── recording.py        # Importing a work: read, declare its vocabulary, adopt what exists
│   ├── runner.py           # One part through the pipeline and back into its file
│   ├── mailbox.py          # What a part could not settle, escalated up the tree
│   ├── reconcile.py        # Reading a wave's rewrites against each other — the link step
│   └── loop.py             # Passes until nothing is outstanding
├── devtools/               # Development CLI, exposed as `lup-devtools`
│   ├── main.py             # Root Typer app composing the sub-apps
│   ├── harness/            # Typed harness declarations — this tree's source
│   ├── trace/              # Trace display, search, and analysis
│   ├── feedback/           # Feedback state, metrics, and session commits
│   └── version.py          # Version display, changelog, and bump
└── environment/            # I/O: how a run starts and what happens to a result
    ├── entrypoints.py      # Every entry point, declared once for all surfaces
    ├── launch.py           # The one path from declared values to a running run
    ├── cli/                # Typer CLI and the interactive chat session
    └── web/                # Session API and the browser surface
```

Keeping `agent/` free of I/O is what makes it improvable: the self-improvement
loop reads traces and changes prompts, tools, and models, and never has to
reason about where a session came from.

## Entry points

A writing session starts in one of five ways — `write` from source material,
`run` from a freeform task, `revise` from an existing draft, `resume` and
`restart` from a saved session — and each declares its parameters once, in
`environment/entrypoints.py`: type, default, help text, and which of the three
surfaces carries it. The surfaces are then compiled from that declaration rather
than written beside it.

| Surface | Rendered by |
| --- | --- |
| Typer commands | `environment/cli/compile.py`, into the checked-in `cli/commands.py`, drift-checked by `dev check` |
| API request models | `request_model`, constructing each at import time |
| Browser form | `GET /api/entry-points`, which the New Session page builds its controls from |

A parameter a surface leaves out has to say why, in the declaration, beside the
parameter — which is what makes the omission a decision on the record rather
than the drift nobody notices. Both environments hand the values they collect to
`environment/launch.py`, the only place the declared set is unpacked, so a
parameter reaches the pipeline without being threaded through either.

## Pipeline stages

Declared in `agent/stages.py` as a prompt and a tool list each, executed by
`agent/pipeline.py`. The stage name is what a cost sink and a trace label are
keyed on, so it is the unit the feedback loop reports against.

| Stage | Produces |
| --- | --- |
| `book_planner` | The book's chapters, their reading order, and the cross-references between them — above the plan, and only for a run assigned to a book |
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

A book's chapters are one run each, so the book stage above the plan is what
owns the order and the cross-references no single chapter could settle for
itself, and it writes them into the book's own record rather than into the
session. `inkwell chapter <book>[:<n>] <sources>` skips it: a chapter written
on its own reads the recorded order rather than laying one out again, and where
the book has no record yet it appends and says what it assumed. An ordinal is
an identity — assigned once, never reassigned — because the reader-feedback
export is keyed by numbers readers have already been given.

## Output formats and the checks they declare

An `OutputFormatSpec` in `agent/stages.py` carries a format's prose guidance
and, beside it, the rows that guidance is re-read against. Both come off one
declaration: `get_format_guidance` renders the rows into what the writer reads,
and the rewrite stage measures the same rows off the finished draft, so a rule
the writer was given and a rule the draft was checked by cannot drift apart.
Adding a row to a format is a constructor call in that format's list; adding a
*kind* of row is one class in `agent/format_checks.py`. A row every format
carries — terminology held to what the shared glossary already settled — is one
entry in `EVERY_FORMAT_CHECKS` rather than a copy in each format's list.

Three tiers, one row shape. **Mechanical** rows are data — a threshold and the
guidance sentence they measure — and read the draft structurally through
`agent/prose.py`: blocks from markdown-it, sentences and tokens from the
segmenter in `agent/segmenter.py`. **Judged** rows spend a reviewer on what
counting cannot settle, and land the verdict in the same `CheckRow`, so the
rewrite stage reads one report. **Measurements** reach no verdict at all: where
the guidance asks *for* a pattern rather than against it — contractions, a
question, a semicolon — no count is a fault and a threshold would invent one,
so the row reports its number through `FormatCheck.reading` and the report
prints it as `measured` rather than as `ok`.

The tells that belong to no one format live in `agent/voice_tells.py`. An
inflated copula and an unattributed appeal to studies read the same in a
textbook chapter and a blog post, so the banned vocabulary and the rows that
measure the house voice are declared there once and a format splices in
`VOICE_TELL_CHECKS`. The vocabulary is declared as the groups the voice
guidance itself names, and both readers come off that one declaration — the row
measures `LLM_VOCABULARY`, the writer reads `BANNED_VOCABULARY_PROSE` — so the
list a writer is given and the list a draft is checked against are one thing
stated once.

`Segmenter` is the seam that decides how much a row can know about a sentence,
and syntok fills it: boundaries an abbreviation or a decimal does not fool, plus
tokens a row can match a construction against by position. syntok is here
because it is pure Python and the project floor is 3.14, which no spaCy wheel
covers; `pyproject.toml` declares spaCy as the `pos` extra to record where a
parser-backed implementation drops in. The rows that would read a parse —
inflated predicates, participial tails, the rule of three, and the fragments
`ShortSentences` counts the length of instead — ship as declared detectors over
those tokens, matching the tells the author enumerated, and generalize the day
the seam is filled by a parser without changing.

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

## Reaching a source that refuses us

A sweep escalates rather than insisting: the plain client first, then a real
browser where the source declares one needs it, then the newest capture a public
archive holds where the source declares that fallback. Each rung answers a
different reason a fetch failed, and a source declares only the rungs its own
behaviour has been shown to need.

**The distinction the ladder turns on is who was refused.** A 404, an oversized
body, a page with no article text in it — those are the same for every reader
there is, so asking anybody else spends a request to learn the same thing. A
401, a 403, an anti-bot interstitial: those are about *this connection*, and
another party may not be behind them. `FetchGated` is that distinction as a
type, so only the one place that can act on it has to know.

**An archive is asked, never impersonated.** Nothing spoofs a user agent, solves
a challenge, or pretends to be a browser it is not. Some hosts publish openly
and refuse automated clients anyway — `openai.com` serves `robots.txt` saying
`Allow: /`, advertises its sitemap, and then answers an identified crawler with
an interstitial on some paths — so the gate is on the connection and not on the
content, and a public archive that already crawled the page is not on it. A
source whose `robots.txt` disallows a path has no business being declared here at
all.

**Provenance is recorded rather than smoothed over.** A capture is weaker
evidence than a live fetch: a copy, taken at a stated time, possibly stale. So
the host is always asked first and its answer always preferred, and a document
that did arrive from a capture carries the snapshot URL it came from. A corpus
whose archived documents were indistinguishable from live ones would be one that
quietly dated itself.

`corpus probe <url> --source <key>` runs that whole ladder for one document and
says which rung answered. A source's gate is not a fact that stays settled — an
edge starts refusing or stops, an archive picks a page up next month — and the
alternative to a single-document probe was sweeping thousands of URLs to find
out, which is both slow and, against a host that is refusing, rude.

## A work of many parts

The pipeline writes one piece. A textbook is a tree of them — the AI Safety
Atlas is nine chapters of seven-or-so section files, each holding several
subsections, and the subsection is the unit anybody revises: 201 leaf parts.
Running the pipeline over all of them costs thousands of dollars, and running
it over all of them *again* whenever one changes is what makes a continuous
loop unaffordable rather than merely expensive.

So the loop is **an incremental build system whose compile step is the writing
pipeline**. Parts are targets, what a part leans on is its dependencies,
feedback dirties a target, reconciliation is the link step. That framing
answers what re-runs, in what order, what is cached, and when it is done.

**Dirtiness is derived, never stored.** A part is out of date when somebody
asked for it, when its text moved under it, or when the ledger holds a change
it consumed and has not seen — every one asked afresh against a *build stamp*
recording what the last run was built from. What is persisted is only what
nothing can recompute: a declared standing (`requested`, `running`, `parked`,
`failed`) and the stamp. A build system that persists dirtiness eventually
believes something clean is dirty, or worse.

**Propagation keys on what changed, not on who changed.** A run publishes the
dependencies it moved; a part is reached only where what it consumed and what
moved intersect. That is why a pass settles: a rewrite that redefines nothing
reaches nothing however many parts sit downstream, and two chapters that
depend on each other come to rest because the cycle carries change facts
rather than nodes.

**The graph is dense before any run**, because a work usually declares one.
The Atlas ships 239 `*[Term]: meaning` entries in
`docs/includes/abbreviations.md` that mkdocs substitutes book-wide, so a part
depends on a term exactly when its prose contains it as a word — the work's
own semantics read back, not inferred. Markdown links would have given
nothing: the Atlas has no cross-links, every non-http target being an image.

**The ledger is handed to the run, not reconciled after it.** A part's run
coins into the work's term ledger directly — one file per part, with the
authors' declared vocabulary read first — so a writer asking what the book
already calls something is answered by the book while the prose is being
written. First-definition-wins then settles at the coining rather than at a
reconciliation afterwards, when the rival name is already on the page, and the
authors outrank every run without a rule saying so: the read order is the rule.
What a part newly coined, or kept the name of while replacing the meaning, is
what it publishes as having moved.

**Adoption is what makes it affordable.** A work nothing has built is a work
where every part is out of date. `manuscript import` stamps each part as built
from the text it already holds — `make -t`, and the same argument — so the
first useful pass rewrites what somebody asked about rather than the book. The
full pass stays available and stays expensive; it stops being the entry fee.

**The work declares the format, not the run.** A part handed to the pipeline
with no format declared falls to `auto`, which asks that part's own extract
stage to infer the shape of a book it is shown one subsection of — and two
parts answering differently is how a textbook acquires a chapter that reads
like a blog post. `manuscript import --format` records it once on the work and
every run against it inherits it, defaulting to `textbook` because a work read
as chapters holding sections holding subsections is one. That is what carries
dependency order, definitions-before-use, and self-containment into a part's
run; an undeclared format is refused where it is typed rather than downstream,
since nothing downstream refuses it — it simply carries no rules and says
nothing.

**A rewrite splices.** One part's span is replaced and its siblings stay
byte-identical, because a model asked to reassemble the file re-emits prose
nobody asked it to touch. Over 201 parts revised repeatedly that is the
difference between converging on the author's book and walking away from it.

**A rewrite that lost its heading is refused, not written.** Spliced in, prose
without the part's own heading leaves the file holding no part where the tree
records one: the next sweep reads it as text the author deleted, the standing
goes unrunnable, and the prose has merged into whichever part precedes it,
where a later revision of *that* part will rewrite it as its own. Nothing about
the file looks wrong afterwards, which is why the heading is checked before the
write — off the parser, so a fenced heading does not count, and a rewrite of
the wrong part does not either. The part keeps its text and the run fails
saying what came back.

**Parked is not failed.** A run that cannot settle something asks, its part
parks, and nothing retries it until somebody answers — through the CLI or the
work's tree in the web surface. A failed part stays failed with what it said,
because one that quietly returned to idle would be picked up next pass and
fail identically forever. Both standings are deliberately sticky, so both need
a door: `manuscript clear` is somebody saying they have read the failure or
changed their mind, which is the judgement the loop cannot make for itself.

**A missing checkout is one fact, not two hundred.** A part whose file or
heading is gone reads `source-gone` rather than as edited prose: the repair is
a checkout to restore or an import to redo, not a rewrite, so the loop declines
to schedule those parts instead of spending a turn each to fail, and a sweep
reports them apart from what is runnable. A work blocked in every part is
stopped, not settled. Every command that reads a work's prose leads with one
line naming the directory that is not there.

State lives in inkwell's own store beside the corpus and the book records,
never in the work's repository: that tree belongs to whoever writes the book,
and state left there would not survive a fresh clone.

**The browser drives the loop, it does not merely watch it.** A work is a REST
resource of its own rather than a view of whichever run is open: the tree with
every part's state, what a run *would* pick up before anybody pays for it, a
start that runs the loop in the background reporting each pass as it lands, and
a stop. One loop per work, refused rather than queued, because the loop is the
single writer of a work's state and two over one book would each lease parts
the other had leased. The preview is not a convenience — every part a pass
picks up is a whole pipeline run, so a start button offered without one would
be offering to spend an unknown amount on a click.

**A pass can be kept to the parts somebody named**, which is the thing an
author asks for most and the one thing a limit cannot express: a limit takes
the first parts in tree order, so cutting a pass to one runs whichever part
comes first in the book rather than the one being asked about. `--part` on the
command line and a row's own *run this part* in the browser narrow the sweep
instead. Narrowing rather than overriding is what keeps one way to ask for a
revision: a named part still has to be outstanding, carrying the reasons it
would have carried in its turn, so `request` remains the door to a part that is
already up to date — and a named part the sweep passes over is reported with
what is keeping it, because up to date, held by another run, parked on a
question, and not a part of this work are four different things to do next that
all look like a settled book from a pass that ran nothing.

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
