# Diagnosis — Author directives don't bind (session `b78c80527a464a38`)

*Investigated 2026-06-14. Session ran 2026-06-13 10:14 → 2026-06-14 08:33 (~22h, across deliberate restarts).*

## TL;DR

The task was to rewrite the author's **own** 283-page PhD thesis as an independent,
simplified arXiv paper. The final draft instead **cites the thesis (`[Mil16]`) 29 times
and delegates every hard proof back to it** — the exact failure the author had explicitly
mocked in the task input. The same run also re-introduced other flagged defects (dim-1
over-repetition, reused private abbreviations), so this is a **class failure, not a
one-off**: author directives are consumed once at planning and are **never the evidence
base for any downstream decision**. Three structural seams (wrong evidence base, fail-open
defaults, re-asking pre-answered questions) compose into the failure. A "hard gate" is the
wrong fix — the wound is missing evidence at the decision points, not a missing prohibition.

A separate end-of-run crash (`exit code -2` / SIGINT, uncaught) lost the final delivery,
and the academic LaTeX path is structurally broken (`tectonic` is not apt-installable).

## The task (and why it is structurally unusual for Inkwell)

From `snapshot_preprocess.json` (`raw_sources`, author writing in French): produce an arXiv
paper with the `FO[ℕ,<,mod]`-definability decision algorithm, the formula-construction
algorithm, both proofs, and a generalization note — **simplified** relative to the source.

- The source `these_7.pdf` **is** Arthur Milchior's PhD thesis. `[Mil16]` **is that thesis.**
- The author **is** the byline. The task input **includes the author's own feedback** on
  the previous iteration, including (verbatim):
  - *"A full development of this decomposition appears in [Mil16, Chapter 8]." → "Lol"*
  - *"si tu renvoies à ma thèse pour les détails, tu peux faire bien plus court pour prouver
    le résultat de ma thèse"* (don't refer to the thesis for details; it can be much shorter)
  - *"L'introduction … de juste redire ce qui est dans mon introduction … n'a aucun intérêt"*
  - *"il répète 6 fois au moins que la dimension 1 est triviale"*
  - *"il utilise TOP, QUAD, sans expliquer … Je les ai créés dans mon papier. Y a 0 raison de
    reprendre les mêmes noms"*

Inkwell's model is *source material → article that cites its sources*. This task is
*transform THIS document into an independent work* — the source is the substrate, not a
citable authority. Nothing in the pipeline represents that relationship.

## Symptoms reported

1. **Crash at the end:** `TeX tooling install failed: E: Unable to locate package tectonic`
   → `LaTeX build incomplete` → `Fatal error in message reader: Command failed with exit code -2`.
2. **Source unreachable from sandbox code** ("missing a mount point?").
3. **Final draft cites `[Mil16]` constantly**, delegating the rewrite back to the thesis.

---

## #3 — the headline failure: author directives don't bind

### It is real and general (Trace 1)

Final content (`snapshot_format.json`):

- `[Mil16]` appears **29×**, delegating deliverables: *"Minimality follows … [Mil16, Theorem
  8.3.7]"*, *"The full argument spans approximately twenty pages of case analysis [Mil16,
  Section 8.2.2]"*, *"A naive enumeration algorithm ([Mil16, Algorithm 10.1.1])"*. The draft
  even regenerated the exact sentence the author had mocked (*"A full development of this
  decomposition appears in [Mil16, Chapter 8]"*).
- **Other flagged directives also recurred:** `"trivial"` **7×** (author flagged ~6×),
  `tops`/`TOP` **342×**, `QUAD` **57×** (the private abbreviations he said not to reuse).

→ The failure is a **class** (author directives don't bind), not specific to `[Mil16]`.

### The failure chain (stage by stage, with evidence)

1. **Preprocess** — the directive is present in the raw task. ✓
2. **Directions are a plan-stage-only input.** `collect_preexisting_directions`
   (`pipeline.py:2992`): author directions are *"injected into the **plan stage** prompt."*
   They do not travel further by design.
3. **Plan understood it, then demoted it.** It quoted the French back, but recorded the
   imperative as an **open menu** (plan snapshot): *"(a) present the full proof … (b) present
   a proof sketch … and refer to the thesis for details, or (c) attempt to find a shorter
   proof. Option (b) risks the same problem you flagged…"* An imperative became option (b),
   still on the table.
4. **Raised as a pending question to the author**, never answered (`events.json`, first event
   of the resumed run): *"Please advise how much compression is acceptable here."* The async
   model does not require the author to answer.
5. **Resolve judged it against the SOURCE.** `stage_resolve` (`pipeline.py:3842`) resolves
   open questions from the source document; judgment calls are marked author-only. It tagged
   this *"a presentation/scope choice the source cannot determine"* — then editorialized
   (`resolutions.md:65`): ***"All three options (a) full proof, (b) sketch + cite thesis, (c)
   appendix are faithful to the source."*** Citing the source is trivially "faithful to the
   source"; the author's directive forbidding (b) was **never in the resolver's evidence
   base**. Marked author-only, unanswered → **no block.**
6. **Write never had the directive.** `write_section` (`pipeline.py:1209`) gives the writer
   its section title, `build_neighbor_context` (adjacent titles+summaries only,
   `pipeline.py:4223`), a live-feedback file, voice files, and prominent academic-format
   guidance. The author directive appears **0×** across the entire write/review/rewrite
   trace. The writer reasoned from scratch (`195933.md:15517`): *"since this is the author's
   own thesis work, I should reference it consistently with the paper's citation style… like
   '[Mil16, pp. 170-190]'."* — the **default academic norm** (you do cite your own prior
   thesis) filling the vacuum.
7. **Review ratified it.** The reviewer (`195933.md:26439`) offered *"renumbered … **or
   consistently cited as references to the thesis, like '[Mil16]'**"* and called a 20-page
   proof compressed to 15 lines *"reasonable for a paper format"* (`195933.md:26437`).
8. **Rewrite/format** polished the delegation into the final 29 citations.

### Root cause — three structural seams

The components are each individually reasonable; they **compose** into the failure.
Unifying statement: **author directives are consumed once (at planning) and are never the
evidence base for any downstream decision** — every deciding stage reasons against the
*source* or the *academic-format norm* instead.

1. **Wrong evidence base.** Directive-questions ("reproduce vs cite", "which abbreviations",
   "how much dim-1") are decided against the source, which is biased toward leaning on the
   source. The author's feedback should be the *privileged* evidence base for these.
2. **Unanswered author-judgment calls fail *open*.** They degrade to whatever the draft
   already does (the easy path) instead of blocking or taking a conservative default. The
   async author model has no fail-safe.
3. **Pre-answered questions get re-asked.** The author already answered "don't cite, do it
   shorter" in the task input; the pipeline re-raised it without connecting the existing
   answer, so the answer could not bind.

### Why not a "hard gate"

The wound is **missing evidence at the decision points + fail-open defaults**, not a missing
prohibition. A blind "never cite the source" check is a rule that would coexist with the same
gap. The durable fix is the "provide state/context" column: make author directives present
and privileged where decisions are made, and make unresolved author-judgment calls fail safe.

---

## #1 — the end-of-run crash

### Timeline (`events.json`, resumed run)

- `06:55:39` resume (reusing GDoc) → `resolve` (25 open questions)
- `07:34:08` *"Resolved 17 question(s) from the source"* → `rewrite`
- `07:58:50` `format` → `08:10:40` *"Building LaTeX artifacts (pandoc + tectonic)"*
- `08:10:41` *"LaTeX build incomplete — shipping markdown only"*
- `08:33:46` last event — crash during the ~23-min final GDoc write

### #1a — TeX toolchain is structurally broken

`latex.py:21` runs `apt-get install … tectonic` at the **end** of the run. The sandbox base
is `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` (`sandbox.py:391`); **`tectonic` is not an
apt package** there (it ships as a static Rust binary), so the install can never succeed.
`pandoc` *is* apt-installable. For arXiv the deliverable is `paper.tex` (server-side compile),
so local PDF compilation is only an author preview — not on the critical path.

### #1b — the crash is uncaught; resume exists but doesn't auto-finalize

`exit code -2` = the main `claude` CLI subprocess was killed by **SIGINT** (SDK
`_internal/query.py:221` wrapping `subprocess_cli.py:581`). `background.py:215-221` **already**
treats `exit -2` as benign for background agents; the **main pipeline client has no equivalent
handler**, so the same signal is fatal. The deliverable content survived (snapshots persisted
at 08:10). Resume is **stage-granular** (`pipeline.py:2680-2682`: resume from the stage after
the last saved snapshot — confirmed by the 06:55 resume restarting at `resolve`), so a manual
resume **would** re-run `format`/delivery. Gaps: requires manual resume, the LaTeX would fail
again, and there is no finalize-on-interrupt to ship what already exists. The 08:33 SIGINT was
an unexpected interrupt during final delivery (not the `check_and_maybe_restart` machinery,
which only fires after research/write/review — all already passed); its exact sender is not
logged.

---

## #2 — source not mounted in the sandbox

Mounts (`pipeline.py:2405-2408`): `/workspace` (rw), `/shared` (rw), `/notes` → notes base
(ro). The source thesis lives at `/tmp/inkwell_uploads/these_7.pdf` — **mounted nowhere**.
Extracted *text* is reachable at `/notes/.../source_text/`, but the original is not. In this
run the agent dodged the gap via host `Read`, but any in-sandbox code over the source (e.g.
to *verify or reconstruct* a proof computationally — which would reduce the incentive to cite)
cannot reach it. #2 quietly enables part of #3.

---

## Fix directions (not yet committed — decisions still open)

- **#3 (the core).** Make author directives a **persistent, privileged evidence base** at the
  deciding stages (write, resolve, review, rewrite), not a plan-only input. Make unresolved
  author-judgment calls **fail safe** (block, escalate, or conservative default) instead of
  defaulting to the draft. Connect pre-existing author answers to the questions they already
  resolve so settled directives aren't re-asked. *Capability half:* native LaTeX authoring so
  reproducing real math is even possible (markdown can't hold theorem/proof structure, which
  makes "see [Mil16]" the path of least resistance).
- **#1b.** Mirror `background.py`'s `exit -2` grace in the main client; finalize-on-interrupt
  (ship the markdown/snapshot that exists); verify resume re-delivers the final stage.
- **#1a.** Bake `pandoc` + `tectonic` (static binary) into a custom sandbox image (passed via
  the existing `docker_image` param) **or** go pandoc-only and ship `.tex`; probe tooling at
  sandbox **start**, not after 22h.
- **#2.** Copy source originals into the already-mounted notes tree (no new mount, no
  cross-session leakage) so in-sandbox code can reach them.

### Open decisions (owner: author)

- LaTeX authoring + collaboration surface: native LaTeX vs hybrid vs harden-current; and what
  the author comments on (compiled-PDF preview vs GDoc-rendered vs batch).
- Scope of the effort (one-off vs class-level hardening) and the validation signal (the
  session is resumable, so a re-run is feasible).

## Evidence index

- Stage traces: `notes/traces/b78c80527a464a38/*.md` (citation reasoning at `195933.md:15517`,
  reviewer at `:26439`/`:26437`)
- Snapshots + artifacts: `notes/traces/0.2.0/sessions/b78c80527a464a38/pipeline_notes/`
  (`snapshot_preprocess.json`, `snapshot_plan.json`, `snapshot_format.json`,
  `artifacts/resolutions.md:65`)
- Events log: `notes/traces/0.2.0/logs/b78c80527a464a38/events.json`
- Code: `src/inkwell/agent/pipeline.py` (`stage_resolve` 3842, `write_section` 1209,
  `build_neighbor_context` 4223, `collect_preexisting_directions` 2988, `start_sandbox` 2400,
  resume 2680, `stage_format` 3949); `src/inkwell/agent/tools/latex.py`;
  `packages/lup/src/lup/sandbox.py` (391); `packages/lup/src/lup/background.py` (205-221)
