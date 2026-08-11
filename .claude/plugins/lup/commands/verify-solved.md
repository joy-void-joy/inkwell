---
description: "Check every claimed-resolved note against what it actually asked"
---

Every `# lup: solved:` marker in this repository is a claim that a note was addressed, made by whoever addressed it. You are the check on those claims, and you are the only thing that may retire one.

Run `uv run lup-devtools dev comments` and read the "Claimed resolved" section. Each entry carries the note's original words, unchanged — that is what makes it checkable, and it is what you judge the code against.

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

Give the rundown before you edit anything: one line per claim naming the file, the site, what was asked, what you found, and your verdict. Then apply every verdict in one pass so the tree matches what you reported.

Say plainly how many you restored and why. A pass that retires everything it reads is not evidence the work was good; it is the first thing to be suspicious of in your own output.
