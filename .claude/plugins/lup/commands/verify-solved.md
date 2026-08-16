---
description: "Check every claimed-resolved note and stale open issue against what it actually asked"
---

Every `# lup: solved:` marker in this repository is a claim that a note was addressed, made by whoever addressed it. You are the check on those claims, and you are the only thing that may retire one.

Run `uv run lup-devtools dev comments` and read the "Claimed resolved" section. Each entry carries the note's original words, unchanged — that is what makes it checkable, and it is what you judge the code against.

The tracker holds the same kind of claim from the other direction. An issue left open after its fix landed asserts something about the tree that is no longer true, and the next run's intake takes it as evidence and plans a concern to solve it again — one leased worktree per issue already fixed. So read `uv run lup-devtools dev issues` in the same pass and judge each one the same way: against the report's own words, not its title. Verify the mechanism in the code, not the commit subject that claims it; a commit whose message names an issue is where to start looking, not proof. Where a report lists several defects under one number, each is judged separately and the issue stays open while any of them stands.

Closing an issue is a claim in public, so it carries its evidence: name the commit and the mechanism, and say what you verified. Never close one on a title match.

## What you are deciding

For each claim, one question: **does the tree now do what the note asked?** Not whether the code improved, not whether the claim is plausible, not whether the agent tried. Read the note's words, then read the code at that site as it stands now, and answer whether the specific thing asked for is true.

Three answers, and no others — each applied with the pass's own instrument, `uv run lup-devtools dev comments`, because the edit gate denies changing a claim marker in any session:

- **Resolved.** The tree does what was asked. Retire the claim: `dev comments --retire <file>:<line>` deletes the marker and its text entirely.
- **Not resolved.** Restore it to open feedback: `dev comments --restore <file>:<line>` strips the `solved: ` head so it reads `# lup:` again, keeping the original words — rewriting them loses what was actually asked. If your reading found something the note did not say, that is a *new* note, written separately.
- **Partly resolved.** Restore it narrowed: `dev comments --restore <file>:<line> --narrow "<the part still outstanding>"`. A claim that answered two of three concerns is not resolved, and carrying forward the two already answered wastes the next reader's time.

Both flags refuse any target that is not a `solved:` claim, so neither can touch open feedback or parked work.

Bias toward restoring. A claim you cannot confirm from the code is not confirmed, and leaving a note open costs one more pass while retiring one wrongly loses the concern permanently.

## What to distrust

- A claim whose text was edited. The words should match the note as written; changed words mean the claim answers something other than what was asked, and that alone is grounds to restore it.
- A claim at a site the diff never touched. If nothing changed there, the resolution happened somewhere else or not at all — find where, or restore it.
- A claim that reads as a summary of work ("refactored the cache layer") rather than an answer to the note. The note asked something specific; the claim has to meet it.
- A note that asked a question. Those resolve by a definitive answer reflected in the code, the docs, or a recorded user decision — not by the code changing shape near them.

## Reporting

Give the rundown before you edit anything, and give it from scratch. A verdict handed over on its own cannot be judged — only accepted on trust — so for each claim explain the underlying problem as though the reader has none of your context, then what you found, then your verdict and the option you would take. Prefer slow and complete over brief: the reader is deciding, and a decision made without the reasoning is one they have to re-derive later. Then apply every verdict in one pass so the tree matches what you reported.

Correct yourself out loud when the check reverses an earlier reading, including your own. A claim you first read as met and then found wanting is the most valuable thing this pass produces, and burying it in a revised summary wastes it.

Say plainly how many you restored and why. A pass that retires everything it reads is not evidence the work was good; it is the first thing to be suspicious of in your own output.
