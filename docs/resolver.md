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
`--accept`/`--reject` is not a separate path: acceptance is the reserved
`integration-acceptance` question, so every door records it the same way. See
[supervisor.md](supervisor.md).

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

The lifecycle inventories concerns, publishes material questions, persists
eligibility and integration approval, validates the dependency DAG, leases a
non-overlapping branch/worktree per approved concern, and executes independent
topological nodes concurrently. Root nodes start from the source snapshot,
single-parent nodes start from the verified parent commit, and multi-parent
nodes use an orchestrator-prepared semantic join.

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
