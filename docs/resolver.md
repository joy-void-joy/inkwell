<!-- Generated from lup.devtools.harness.content.docs.resolver by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Resolver lifecycle and recovery

`ResolverCore` is the single provider-neutral resolver. Concrete entries inject
worker and reviewer factories, a native skill invocation renderer, a process
launcher, and an explicit state root. Environment variables never select a
resolver implementation.

Run the native composition directly with one of:

```text
uv run lup-devtools harness resolve --adapter claude
uv run lup-devtools harness resolve --adapter codex
```

`--run-id` selects a stable run for explicit recovery. Without it, the command
uses the current source commit. The generated Claude and Codex entries only
launch these composition roots; they contain no resolver phases, and both
render the same direct CLI instruction.

## Questions are files, and every door writes to them

A run holds `flock(LOCK_EX | LOCK_NB)` on `.run.lock` for its entire life, so
nothing outside its process can take that lock to hand it an answer. Questions
and answers are therefore files under the run directory:

```text
questions/<question-id>.json   # write-once; workers and the core
offers/<question-id>.json      # anyone writes; last-write-wins; correctable
answers/<question-id>.json     # the run's promoter only; exclusive create
park.request                   # any door aborting every open wait
```

Three directories rather than two, because nothing under `.lup/resolve` is
ever unlinked: letting a door write `answers/` directly would make a mistyped
free-text value permanent. `offers/` is the correctable layer, and exactly one
promoter — inside the run — takes the earliest valid offer, which turns "first
answer wins" into a deterministic decision instead of a filesystem race. An
offer may legitimately arrive *before* its question exists, which is what lets
`--answer` answer a question a fresh run has not asked yet.

One file per question, written with an atomic rename, so no door needs a lock.
That is mandatory rather than convenient: every door must write while the run
holds its exclusive lease.

`--wait <seconds>` decides how long a run blocks before parking, and defaults
to `0` so an unattended invocation is deterministic. Nothing is inferred from
the environment. Wait and poll settings are constructor arguments rather than
`ResolverConfig` fields, because `resolver_config_digest` gates resume — a
`--wait` in the config would make every rerun with a different wait fail to
resume its own run.

The doors are `--answer <question-id>=<value>`, the supervisor page,
`lup-devtools harness resolve answer`, and a worker's own question tools.
Assembling the review branch has no door of its own: it is the reserved
`integration-assembly` question, answered `approve` or `defer` through
whichever of those a human is already at. See [supervisor.md](supervisor.md).

Accepting a concern *over a failing verification* is a different decision and
has its own command, `lup-devtools harness resolve accept`, because it records
a reason in place of a green check that was never green.

**Telling an actor something is a stream, and delivery is a position.**
`resolve say` rides in front of the actor's next tool call; `resolve redirect`
refuses that call and hands back the text as the reason. Both are recorded
against the actor that received them, and both are consumed exactly once
through one position per conversation, kept in the run directory rather than
in whichever session happens to be open — a reader starting at the stream
head begins *after* everything posted while it was away. An actor recognizes
every spelling of itself, so the label `resolve actors` prints reaches it
whatever round it has moved on to. Because a door writes the stream and the
actor reads it later, `say` and `redirect` report what a message is queued
for rather than that it sent, `actors` lists what each actor has not read
yet, and anything still queued when a session closes is recorded.

**`state.json` lags the mailbox.** The run folds `questions/` and `answers/`
into `state.questions`/`state.answers` as it promotes, so those copies are
correct once a run finishes and behind while it moves. The rule: the mailbox
is authoritative for anything pending; `state.json`'s copies are a fold.

State lives at `<state-root>/<run-id>/` as an atomic schema-versioned
`state.json` plus concerns, questions, answers, leases, dependency bases,
agent rounds, reviews, and integration projections. Restart must load only that
explicit run and verify recorded branches, commits, worktrees, and leases.

For a new run, the composition root scans tracked files for actionable review
notes and passes their source context to a read-only structured planning turn.
The planner must assign every note exactly once to a generalized concern. If a
note-bearing file differs from `HEAD`, an unattached Git commit captures those
files through a temporary index. The user's branch, `HEAD`, index, and working
tree are unchanged.

**That inventory is readable before a run exists.** `lup-devtools harness
resolve intake` prints the partition a run would plan from — each actionable
note at its file and line, the deferred ones it would carry, and the ones it
would leave to a generator, named by the semantic id that owns them — while
creating no run and leasing no worktree. Every other subcommand operates on a
run that already exists, so whether a run is worth starting was answerable
only by starting one, which leases a worktree per concern. It reads through
the same partitioning the run itself uses, so a preview cannot show one thing
and a run plan another.

The lifecycle inventories concerns, publishes material questions, persists
eligibility and integration approval, validates the dependency DAG, leases a
non-overlapping branch/worktree per approved concern, and executes independent
topological nodes concurrently. Root nodes start from the run's base,
single-parent nodes start from the verified parent commit, and multi-parent
nodes use an orchestrator-prepared semantic join.

**Evidence has three kinds, and the tracker is one of them.** A run plans
from `# lup:` notes in the tree, statements a human typed, and the project's
open issues — every one minus an exclusion label, so what goes unlabelled
still gets read rather than what goes unremembered going unfixed. Positions
run end to end in that order, so a planner cites any kind the same way and
clusters issues exactly as it clusters notes: one issue routinely raises
several concerns and several routinely raise one. The library knows an issue
only as a number, a URL and some text; reaching a tracker is devtools' job,
so a project on another forge supplies its own reader. When a concern
derived from an issue lands, the run comments there naming the review branch,
and never closes it — a reviewer passing is not a human having read the code.

**Statements seed a run as well as join one.** `--admit <text>` carries work
in the human's own words into a live run, and where no run exists yet it opens
one from those statements beside whatever notes the tree already holds. Both
are positions in the same request, so a seeded run and a scanned one reach the
same shape of inventory and one run may mix them. Otherwise somebody arriving
with the concerns in their own words — which is how a human arrives — had to
invent a note site for the planner to read back, a file edit standing in for a
sentence.

**A base is refreshed, not only inherited.** The base starts as the source
snapshot and is brought up to the branch it came from whenever a lease is
created — fast-forwarded where the snapshot is contained in the branch,
merged where it is not, so a run planned from uncommitted notes keeps them.
That is the one moment it costs nothing: the worktree does not exist yet. A
lease already holding work keeps its base until somebody asks, with
`lup-devtools harness resolve refresh --run-id <id>`, which reports per lease
what merging would conflict on and takes it only with `--apply`. A concern
whose work is already verified is never moved: its commit is what the run
records and joins.

Combining the two bases can itself conflict, and ordinarily does: the fix
that unblocks a parked run touches the files that run's notes are about. So
the refusal names the paths, and `--base <commit>` adopts a combine somebody
resolved by hand — checked to contain both the run's base and the branch, so
a resolution that dropped one side is refused rather than taken. One
resolution there replaces the same conflict met again in every lease.

Verification is scoped by the tree it runs on rather than by the run. A
command declares the flag it takes a base through — `--since` for `dev
check` — and the run supplies the commit of whatever tree is being verified.
A base written into the arguments instead would sit inside the digest that
gates resume, so a run could not resume itself once its base moved.

The initial question batch includes an approve/defer decision for each planned
concern. A directly approved concern is still ineligible when any dependency is
deferred. These answers, rather than planner guesses, control the work DAG.

Workers may edit only their leased root and never create branches or commits.
A worker raises a material question through its own tools and keeps working,
rather than ending its turn to report it — so an answer costs a tool call
instead of a whole new session, and a question nobody answers in time parks
the concern exactly as a headless run always did. The tools bind the concern
id in a closure, so a worker structurally cannot post against a sibling.

The orchestrator validates the diff and creates the real commit, then an
independent reviewer checks every persisted acceptance criterion. Bounded
revision rounds are persisted. The orchestrator strips a concern's own
markers from its lease before the worker starts, as a dedicated commit, so
the worker never edits a marker and never trips the gate that asks on every
marker-count change. Clearance matches note identity rather than file, so a
sibling's note in the same file — and every deferred note — stays put. Only
verified commits reach the dedicated review-master worktree.

Integration runs configured verification commands, requests an independent
final typed review, records cleanup or retained-worktree instructions, and
stops at human acceptance. It never merges into the user's branch. Failure
records partial evidence and retains actionable cleanup state.
