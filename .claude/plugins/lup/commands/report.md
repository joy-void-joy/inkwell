---
description: "Write the report of everything left to implement, rewritten whole under tmp/, after a long session or after implementing a plan"
allowed-tools: Bash(uv run lup-devtools:*), Read, Write
---

# Report what is left

Write down everything there still is to implement, at `tmp/report.md`.

Two occasions, one output. After a long session, the report is written from
scratch: what the session opened, what it left half-done, and what the
surfaces can still see outstanding. After "please implement
tmp/plan_something.md", the same report is written again from scratch, saying
what remains of that plan — **rewritten whole, never appended to**, so a line
that is no longer true cannot survive by being further down the file.

## 1. Ask the surfaces

```bash
uv run lup-devtools report --write
```

This writes the half no session can see from memory, reading every surface
that already answers for one topic:

- **Open notes** — Review feedback still asking for something, at the site it concerns.
- **Deferred work** — Work parked at its own site, with the gate that would wake it.
- **Unverified claims** — Notes claimed solved, awaiting the pass that retires one.
- **Stale artifacts** — Generated trees the typed source has moved out from under.
- **Unlanded branches** — Work committed where the integration branch has not taken it.
- **Resolver leases** — Concerns a run is holding, which nothing else should pick up.

It reports what this tree can act on: a note inside a generated artifact is
the same note twice, so only the source one counts.

## 2. Read what it wrote

Read `tmp/report.md`. Those sections are the ground truth about the
repository — do not re-derive them, contradict them, or drop one because it
was empty. An empty topic is an answer.

## 3. Rewrite the file whole

Write `tmp/report.md` again, in full, with every section from step 1
intact and one more added at the end:

```markdown
## From this session

- What was implemented, and what of it is unfinished.
- What was decided and has not been written down anywhere a scan can see.
- What the next session has to do first, and what it needs to know to do it.
```

Where a plan was being implemented, that section is what is left of the plan:
each item still open, and for each, where the work stopped. An item that is
done does not appear — the report says what is left, not what happened.

Use `Write`, not `Edit`. Rewriting whole is what keeps the file honest, and
an edit to one line of a stale report leaves the rest of it stale.

## Guidelines

- **`tmp/report.md` is the only file this writes.** It is scratch:
  gitignored, so it reaches no diff, no reviewer, and no commit. Never create
  a second one — a `TODO.md`, a backlog, a roadmap — because a tracking file
  parks work where no workflow will surface it again.
- **Never append.** A report that grew is a report nobody can trust the top
  of. Every invocation replaces the whole file.
- **Work that belongs in the code goes in the code.** A request about one
  site is a `# lup:` note there, and work deliberately parked is a
  `# lup: defer:` note there. Step 1 then finds it every time, where a line
  in this file would be found only by whoever opens it.
- **Say what is left, not what was done.** A summary of the session belongs
  in the reply; this file is read by whoever picks the work up next.
