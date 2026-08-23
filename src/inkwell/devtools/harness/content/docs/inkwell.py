"""Guide to ``src/inkwell``, the writing agent built on the lup framework.

What the guidance points at rather than carries: the tree, what each pipeline
stage is for, and what this repository considers worth testing. All of it is
reference a reader wants at one particular moment, which is why it is a page
rather than always-loaded prose.
"""

import lup.harness.models as models

DOCUMENT = models.PromptDocument(
    source=__name__,
    parts=[
        models.TextPart(
            text=r"""# The writing agent

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

### Distilling: the corpus de-duplicates documents, not conclusions

One subsection's research cost $27 and 32M input tokens, and almost none of it
went on *finding* things: 42 of its 120 sources came off disk, and every "what
happened this year" question was answered from the corpus. It went on **reading**
them. The next subsection reopens the same evaluations paper and re-derives the
same numbers at full price.

So `distil` is a fourth stage, and what it produces is keyed on
`content_sha256` — a document's findings are a function of its bytes, so a
document that changes source, gets re-slugged, or is re-fetched unchanged reuses
what was read of it, and two sources holding one paper read it once between
them. A finding is a claim as far as the document supports it, **the caveat its
own authors attach** (the half that gets lost — a number travels and its
conditions do not), a verbatim quote, and a locator.

It cannot ride the judging pass, which reads six pages on purpose because the
front matter is what says what a document is *about*; a finding is what it
*establishes*, and that is on page 34 as often as on page 1. And it runs lazily,
a document read the first time a filter reaches it, because eleven hundred
documents distilled up front is a bill for a corpus of which one subsection
needs three hundred.

`manuscript research <work>` is the arrow after that. It distils what the work's
tag filter reaches, asks which parts each finding bears on — reading the tree's
*titles*, never the book's prose, which is what makes it cheap enough to re-run
whenever the book's structure moves and what keeps a single part from triggering
a book read — and turns each new placement into a `finding` change fact. The
ordinary sweep then dirties those parts and the loop picks them up naming the
paper. Two properties matter and are structural rather than watched for: a sync
that places nothing new dirties nothing, so the loop still settles; and the
placement is compared by document-and-claim, so re-running the assignment does
not re-dirty everything it already placed. The reading is priced before it is
spent (`--dry-run` says how many documents the filter reaches that nothing has
read), and publishing is asked about separately, because finding that 34 parts
have gone out of date should not by itself commit anybody to rewriting them.

### Checking a reference, once, for the whole book

A book cites the same pages over and over: 873 citation instances across the
Atlas reach 702 distinct references, and one Our World in Data page carries
dozens on its own. Checking per citation pays repeatedly to reach the same
answer, so a verdict is kept under the URL — canonicalised, so a fragment or a
trailing slash does not buy a second reading — and the first part to cite a page
pays for every part after it.

What is cached is the *reference*, not the claim. Whether a source supports the
sentence citing it is a question about that sentence, and two parts citing one
paper for two different claims are asking two different things; that belongs to
the fact-check reviewer. What is the same for both is what the paper **is** —
whether the URL still resolves, its title, who published it, when, and what it
establishes in a sentence. That is the half a book gets wrong at scale (a dead
link, or a figure attributed to the wrong report) and the half that does not
depend on who is citing it.

`manuscript references <work>` runs it, and a reference that does not hold up is
placed on the parts that cite it as a *bearing* — the same model a research
finding travels as, so it dirties those parts through the ordinary sweep and
reaches the next run's brief with nothing new anywhere. Dead and doubted are
counted apart, because one wants a replacement and the other wants the citation
corrected. A check that could not be made records nothing, unlike a distillation
that found nothing: a reference the network refused today is one to try again,
not one to record as dead.

A readable source may still carry a caveat — a preprint's standing, a live
page's revision history, or a claim its authors state narrowly. That is stored
apart from `note`: a caveat travels with a sound reference, while `note` is
reserved for a disqualifying destination such as a listing, missing document,
or unreadable paywall. Mixing the two would turn every careful qualification
into an instruction to rewrite the part that cites it.

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

**The work is readable, not merely referred to.** A part run has its own span
as its material, and is asked not to reintroduce what the book has already
established — a rule about text it can only follow if it can read that text.
So the instruction names the work's root and the files of the parts a reader
reaches either side of this one, and the run reads them with the tool it
already has. What this replaces is a run that went looking for the work where
it *could* reach it: the published edition, which on a book mid-revision is a
different version with different chapter numbers, so what came back was a
revision measured against a book this is not.

**The part is written, not anchored — and then inherited from.** The standing
text reaches the run as `source`, material to write from. Routed as
`revision_target` it became the plan instead: the planner is shown the piece it
replaces, so its sections are the planner's sections, and one subsection came
back with all seven of its headings in their original order, thirty-four new
ones hung off them, and the most pressing development in the section fourth
because a heading was already fourth. Twenty-five of its thirty research
questions opened "The draft says…". The warning beside that instruction said
nothing in the draft was settled by having been written, and the concrete
instruction won, as it always does — which is why the fix is where the material
is routed rather than in the wording.

What that trade costs is paid in one place. A draft written from scratch keeps
what it happened to keep, and the standing text is somebody's book: figures the
work numbers, citations somebody chased, claims nobody restates for free. So the
fresh draft is not what goes into the work — the *inheritance pass* reads the
two against each other and writes the successor, and every figure and citation
the standing text carried either survives or is named as dropped with a reason.
That binds because it is arithmetic: the audit is subtracted from what the two
texts actually carry, so a pass that dropped three citations and mentioned none
of them is told exactly which three and asked again, up to three attempts,
before the part fails with them named. Both drafts and the audit stay in the
run's room, so which of the two went into the book is answerable afterwards.

Three things travel with the run that only the work knows: its figures with the
numbers the book gave them and the paths they load from, because Figure 2.13 is
the thirteenth figure of chapter two for reasons in chapters this run cannot
see; a *length budget* read off its chapter, because a run asked how long a
subsection should be answers from the subject and the subject is inexhaustible
— 1,023 words in, 8,321 out; and what the reasons for the run license, because a
part told nothing infers its scope from the size of what it is about.

**The plan comes from outside the run too, for the same reason.** A run holding
one subsection planned that subsection's existing sections, because that is all
it could see — and twenty-five of its thirty research questions opened "The
draft says…", which is what a planner shown only the draft has to ask. So a
*brief* is composed first, from the part, the two files either side of it, its
budget, its figures, and its slice of what the corpus established, and the plan
stage records that instead of deriving one. It never opens the book: the bound
is what keeps testing one subsection from costing a book read, and the other
producer of the same artifact — a planner that reads the whole work and emits
every part's brief at once, seeing cross-cutting staleness this cannot — is what
a full pass wants. Both emit an `ArticlePlan`, so nothing downstream knows which
one it got, and a deriver that comes back with nothing leaves the run planning
for itself rather than failing.

There are two producers of that brief, emitting the same `ArticlePlan` so that
nothing downstream can tell which one it got. The per-part deriver above is one.
`manuscript plan` is the other: it reads the whole work and plans every
outstanding part at once, which is what a full pass runs. What only it can see is
what the outstanding parts are about to do to *each other* — three parts each
about to introduce the same paper, a definition ordered after the part that leans
on it, a figure corrected in one place and left wrong in three — and no reviewer
reaches those either, because a reviewer reads one part and asks whether it is
good. It reads the book once for that, then plans a chapter at a time against
what it found: one call emitting 201 briefs is one call to lose, and a chapter is
the unit whose parts actually share anything.

Before a chapter's briefs are stored, four independent readers challenge each
one at the plan tier: whether the ledger reasons license its scope, whether the
standing text already says or silently loses what it proposes, whether an
immediate neighbour owns or must establish the work, and whether a
load-bearing change rests on one source. The readings run concurrently and
report only concrete faults. A final planning turn settles the findings, keeps
an original brief when a repair omits it, and records every concern beside the
briefs so the reason for a changed plan survives the planning command.

Those structured readers keep the work's configured plan model (Opus unless a
caller overrides it) and its full reasoning budget.

A planned brief is stamped with the text it was planned against, so a run uses
it only while its part still holds that text. Reused after the part is
rewritten, it would be a plan for a piece that no longer exists and would pull
the part back toward a draft two revisions old. A part nothing planned derives
its own, so planning is worth running before a full pass and skippable before a
single one.

Recorded rather than skipped, deliberately. A declared skip would save exactly
the same planner call and silently take the Plan tab, the section tabs, and the
plan on disk with it; the first thing to notice would be whichever later stage
opened the file. What a work *does* declare skipped is the stages that are
genuinely optional for it — `manuscript import --skip voice --skip assumptions`
— and a skip naming no stage is refused where it is declared, because a typo
skips nothing and reads afterwards exactly like a stage that ran.

**The document chain runs upward, and is read-only.** A part run makes its own
Google Doc, and made a new one every time it ran — four runs of one subsection
made four documents, and a pass over 201 parts makes 201 more nobody opens
twice. A part's working document is now recorded and reused across its runs, and
what an author actually reads is projected the other way: `manuscript project`
writes each chapter into one document with a tab per part, nested the way the
work is (Docs take three levels of tab, which is chapter/section/subsection),
plus one document holding the whole work. Projecting is idempotent in both
directions — a chapter keeps its document, a tab is written over rather than
added beside — so it is the same command after one part lands and after two
hundred do.

A tab is a part, which is why there is no routing table. A comment lands in a
tab, that tab holds one part's prose, and the passage the comment quotes is a
passage of that part — so `manuscript feedback` traces each comment to its part
by lookup and asks for that part, with the comment as the reason. Traced by the
quoted text rather than by Drive's anchor, because an anchor is a position in a
document rewritten every pass. A comment is taken in exactly once; acted on
twice it would ask for its part again on every sweep and the loop would never
settle. A comment quoting text no part holds any more is reported rather than
dropped.

Read and comment, never edit. Prose is edited in the markdown, where the sweep's
`source-moved` verdict already notices it. A document that were also an editing
surface would be a second copy of the book that could disagree with the first,
and every projection would have to decide which one won.

**Three levels, and the history counts what failed.** A pass reports when it is
over; a part reports through the work's state while it holds a lease; beneath
that, nine writers and six reviewers were doing the work and reporting nowhere
anything outside the run could read. `GET /works/{work}/activity` joins all
three in one reply — the loop, the parts in flight, and each part's agents read
off the cohort roster its run already keeps on disk. One reply rather than
three, because split across three calls a page renders a pass with no parts and
then parts with no agents, and the moment somebody is asking about is the moment
those disagree.

The agents belonging to *no* part were the genuinely missing thing: distilling a
document, placing findings, checking a reference. A pass that spends hours
reading before it writes a word holds no lease on anything, so the work looked
idle — which is the state somebody stops a loop out of. Those are recorded
against the work rather than under a run, because they outlive every run, and
written as each one lands rather than counted at the end, because the window
somebody is asking about is the one before the end.

`GET /works/{work}/history` lists every run, newest first, read off each run's
own room rather than off the build stamps. A stamp exists only where a run
produced prose, so a history read from stamps is a history of successes with
every failure missing — and the failures are the runs most worth opening. Each
room's first write says which work, which part, and when; how far the run got is
which of `produced.md`, `adopted.md`, and `inherited.json` sit beside it.

The work page renders both replies. Its activity panel shows the pass, every
leased part and the agents inside that part's run, plus corpus/reference/planning
readers that belong to the work rather than a part. Its run history links every
attempt to its session and distinguishes produced, adopted, and current-build
prose, including attempts that produced nothing.

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
"""
        ),
    ],
)
