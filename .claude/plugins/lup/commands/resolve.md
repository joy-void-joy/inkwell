---
description: "Resolve inline feedback through isolated work"
---

Deferred notes — `# lup: defer: <text>` — are parked work, not open feedback, and the resolver entry excludes them from its inventory, so an editor can never be assigned one. That bare spelling is the default: nothing evaluates a wake condition mechanically, so `defer[<gate>]: <text>` is reserved for a real, externally-checkable gate ("until the v2 API ships") and never restates that this code might change again. Triage them before launching the resolver: read each note against the current state of the repository — its gate where it stated one, its own text where it did not — and when the work reads as due, propose waking it to the user. Waking is an explicit edit that removes the `defer` head so the note re-enters open feedback on the next run; anything still parked carries forward untouched, never re-litigated.

Run `uv run lup-devtools harness resolve --adapter claude`. The command accepts optional flags: `--run-id <id>` resumes a persisted run and `--accept`/`--reject` records the human decision on its review branch. It waits zero seconds by default and parks on material questions, printing each one beside the `# lup:` notes it was raised from, the concern's spec, and its acceptance criteria; rerun with the repeatable `--answer <question-id>=<value>` flag to answer them. `--admit <text>` admits work discovered mid-run in the human's own words and `--admit-note <file>:<line>` admits a note you wrote in the tree, both repeatable. Never pass `--wait` or `--supervise`; both hold a run open for a human instead of parking — `--wait` at the mailbox, `--supervise` at the page it opens. Launch it with `dangerouslyDisableSandbox: true`. Every session the run opens is a child of this call, and a session spawned inside the sandbox cannot create its own `~/.claude/session-env/<id>` — so each of its shell calls dies on `EROFS: read-only file system, mkdir`, leaving planners and workers unable to run a single command while still appearing to work.

## Relaying a parked question

The run parks rather than guessing, so every material question is a decision that belongs to the human. Never answer one yourself — and never merely transcribe one either. A prompt and a choice list, handed over as printed, reads as a decision with no stakes and cannot be judged.

**Measure before you relay.** The planner writes from notes, not from the tree, so every question's framing is a hypothesis you can check: read the code it concerns and measure what it asserts. A type probe shows whether the "redundant" overload is load-bearing or whether removing it widens every call site. A grep bounds how many sites a proposed rule would actually catch, and how many of those are correct by design. A count of the thing a note named as the worst case can demote it to a false positive outright. Measurement changes the answer often enough that relaying from the planner's prose alone is guessing with extra steps.

**Relay what it takes to judge cold.** For each option: what it means concretely, what it costs, what it collides with elsewhere in this same run, and which of the concern's acceptance criteria it satisfies or fails. Explain the underlying problem from scratch when the question is unfamiliar rather than building on context the human does not have, and put it in the terms they reason in rather than the planner's vocabulary.

**Give your own recommendation, marked as yours.** It is allowed to differ from the planner's, and it should when your investigation says so. When the option set mis-carves the problem, say that and offer the corrected option instead of defending the list — a wrong framing costs a whole extra round even when every detail in it is accurate. The choices are suggestions, not a menu: say so, and pass an answer in the user's own words whenever they give one.

**Relay the whole batch at once.** A run parks with all of its open questions together; asking them one at a time makes the human re-establish the same context for each.

## Work discovered while a run is parked

A parked run is when the most is known about what else needs doing. Admit that work into the run that found it: hand the run the new evidence and only that evidence is planned, so the run keeps its id, every answer already recorded, and every concern already completed. The admitted concern then passes the same approval and material-question gates as one from intake, and may depend on a concern this run has already finished.

The evidence is either a `# lup:` note you write in the file it concerns, which keeps the concern traceable to code, or the human's own words when nothing in the tree carries them; the run records which, so a statement-grounded concern is distinguishable in review. Admission is accepted at any phase before integration — past that the review branch is assembled, and a fresh run is the honest answer.

So a concern discovered mid-run is never dropped and never a reason to restart. Restarting re-derives the inventory from scratch and discards every material answer already collected, which is the most expensive thing you can do at exactly the moment the run holds the most of them.
