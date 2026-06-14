# Fix design — Author-constraint enforcement (session `b78c80527a464a38`)

Companion to `2026-06-14-author-directives-dont-bind.md`. Read that first for the
failure chain. This document turns the diagnosis into fixes, and it **extends the
existing overhauls** in `PLAN.md §0` (source-fidelity) and `§0c` (voice-fidelity)
rather than rebuilding them.

## The precise gap (why the existing machinery didn't catch it)

Most of what a first-pass design would propose **already exists** and *fired*:

- `ArticlePlan.deliverables` is the immutable contract — *"reviewers flag violations
  as critical"* (`models.py:70`). The reviewer enforces it: *"Does the draft deliver
  every item in the plan's deliverables list?"* (`stages.py:229`); the rewriter
  delivers it (`stages.py:154`).
- The planner correctly extracted **every** author constraint — into
  `plan.author_direction`: *"present a self-contained proof… references back to the
  thesis for proof details [a critical problem]… explain TOP, QUAD properly or use
  descriptive names… least significant digit first."*
- Reviewers even **noticed** the violation (`review_style.json`: *"…opaque to any
  reader without the thesis"*).

So why did it ship? **The author's constraints landed in the soft prose field
(`author_direction`), not the enforced contract (`deliverables`).** `deliverables`
got only the *outputs* (`["un papier à mettre sur arxiv", <the two algorithms>, …]`).
There is an enforced checklist for **outputs delivered** and **no enforced checklist
for constraints honored**. Every stage therefore treated "cite vs reproduce" as an
*open author-judgment call* — raised, escalated to the absent author, and (fail-open)
defaulted to the draft's status quo (cite). The same path dropped TOP/QUAD (342×/57×)
and the dim-1 repetition (7×).

**One classification error — constraints filed as context, not contract — defeats the
whole enforcement layer.**

---

## Fix #3 (core) — give author constraints the same enforced channel as deliverables

A single structural change that reuses the contract-enforcement path already proven on
`deliverables`. No new rule, no gate: the agent receives the author's *own* constraints
through the channel that already carries enforcement weight.

1. **Schema:** add `constraints: list[str]` to `ArticlePlan`, sibling to `deliverables`,
   with identical semantics. Field description (the contract — Bitter Lesson):
   > *"Hard constraints the author has stated — prohibitions and requirements the
   > deliverable must honor (e.g. 'self-contained proofs; do not cite the source for
   > deliverable content', 'define or rename borrowed abbreviations', 'LSD encoding').
   > Imperatives, not preferences or context. Immutable through refinement; reviewers
   > flag violations as critical."*
   Today's `author_direction` stays as soft context/voice; the planner promotes the
   **imperatives** out of it into `constraints`.

2. **Planner:** instruct extraction to route author *must/must-not* statements into
   `constraints` (the brief→plan step already copies deliverables; this copies
   constraints alongside).

3. **Reviewer:** one parallel checklist line beside the deliverables check —
   *"Does the draft honor every item in the plan's constraints list? Flag each
   violation as critical."* This is what converts *"opaque without the thesis"* from a
   soft observation into a critical finding that routes through `resolve → rewrite`.

4. **Resolve stage (`stage_resolve`):** give it `constraints` as evidence and have it
   resolve constraint-answered questions **from the constraint**, not from the source.
   "Cite vs reproduce" is no longer source-answerable-or-author-only — the constraint
   *settles* it, so it is never escalated as an open question and never fails open. This
   is the seam fix: a constraint outranks the source as evidence for directive-questions.

5. **Rewriter:** honor `constraints` with the same force as `deliverables` (correctness
   tier).

**What this cascades to (one change, whole class fixed):** `[Mil16]` delegation,
TOP/QUAD reuse, dim-1 repetition, LSD encoding, single-vocabulary scope — every item the
author flagged becomes an enforced constraint a reviewer checks and a rewriter honors,
instead of prose that gets re-litigated and defaulted away.

**Capability complement (academic math):** enforcing "reproduce, don't cite" only helps
if the agent *can* reproduce. Markdown can't hold theorem/proof structure, so the agent
needs native-LaTeX authoring + the source reachable in-sandbox (Fix #2) to satisfy the
constraint instead of hitting a new wall. The constraint fix is general and ships first;
the capability fix is the academic-specific complement (open decision below).

---

## Fix #1b — don't lose the deliverable on interrupt

(Extends the deferred `PLAN.md §0b` item: *"Actionable failure logging… 'exit code -2'
deaths logged nothing usable."*)

- **Interrupt grace in the main client.** `background.py:215-221` already treats
  `exit code -2` (SIGINT) as benign for background agents; the main pipeline client has
  no equivalent. Extract the detection into a `lup` helper (e.g. `is_interrupt(exc)`)
  and use it in both places (DRY; stays in `lup`).
- **Finalize-on-interrupt.** Wrap the stage loop (`pipeline.py:2689`) so an interrupt
  triggers a best-effort `finalize()`: flush the snapshot and ship the content that
  already exists (write the Final tab / emit markdown). The deliverable lived in
  `snapshot.output` at the time of the crash — deliver it instead of dying.
- **Confirm resume re-delivers.** Resume is stage-granular (`pipeline.py:2680`) and
  re-runs `format` if it crashed before the format snapshot saved; make `stage_format`
  idempotent on resume (re-writing the Final tab is fine).

## Fix #1a — academic TeX toolchain (Phase E is broken)

`PLAN.md §0 Phase E` chose tectonic, but `latex.py:21` installs it via `apt` at the end
of the run, and **`tectonic` is not an apt package** on `bookworm-slim` — so it can never
succeed.

- **Provision, don't install at runtime.** Bake `pandoc` + the `tectonic` static binary
  into a custom sandbox image, passed via the existing `Sandbox(docker_image=…)` param
  (inkwell owns the image; `lup` stays generic). `ensure_tex_tooling` becomes a pure probe.
- **Probe at sandbox start**, not after 22h — fail fast when academic format is requested
  and tooling is absent.
- **Deliverable is `.tex`.** arXiv compiles source server-side; PDF is an author preview.
  pandoc (apt-installable) or native-LaTeX authoring produces `.tex`; tectonic is only for
  the local preview.

## Fix #2 — source reachable in the sandbox

(Extends `PLAN.md §0 Phase A`: *"Sandbox mounts: execute_code cannot see
pipeline_notes/artifacts"* — that fix mounted `/notes`; the **source PDF** is still
unmounted.)

- Copy source originals into the already-mounted notes tree at extract time
  (`notes/.../artifacts/sources/these_7.pdf` → reachable at `/notes/.../sources/`). No new
  mount, no `/tmp` cross-session leakage, survives cleanup. Update the compute-tool
  `usage_notes` (`pipeline.py:717`) to name the in-container source path. Unblocks
  in-sandbox verification/reconstruction of the source — the capability half of Fix #3.

---

## Sequencing

1. **Fix #3 constraints channel** — highest leverage, general, small, independent of every
   open decision. Fixes the whole class of dropped directives.
2. **Fix #1b** — small, self-contained robustness; stops 22h runs from evaporating.
3. **Fix #1a + #2** — academic deliverable + source-in-sandbox; pair with the capability
   decision.

## Open decision (owner: author)

- **Native-LaTeX authoring surface** for academic math (vs hybrid vs harden-current), and
  what the author comments on (compiled-PDF preview vs GDoc-rendered vs batch). Fix #3 does
  **not** depend on this; but a *good* academic result on heavy-math tasks does, because the
  enforced "reproduce" constraint needs a surface that can hold the math.

## Proposed `PLAN.md §0d` (tracked checklist)

```
### 0d. Author-constraint enforcement (session b78c80527a464a38)

The pipeline extracted every author constraint into plan.author_direction (soft
prose) but only plan.deliverables (outputs) is the enforced contract — so
"self-contained; don't cite the source" was re-litigated as an open author-judgment
call at every stage and defaulted to citing. Result: 29 [Mil16] citations, TOP/QUAD
reused 342×/57×, dim-1 trivia repeated — the exact defects the author flagged.

- [ ] `ArticlePlan.constraints: list[str]` — enforced sibling of `deliverables`;
  planner promotes author imperatives out of `author_direction` into it
- [ ] Reviewer checks the draft against `constraints` (violations critical), beside
  the deliverables check
- [ ] `stage_resolve` resolves constraint-answered questions from the constraint, not
  the source — a constraint outranks the source for directive-questions, so they stop
  failing open
- [ ] Rewriter honors `constraints` at the correctness tier
- [ ] Main-client interrupt grace + finalize-on-interrupt (mirror background.py
  exit-2; ship snapshot.output); verify resume re-delivers format
- [ ] Academic TeX: bake pandoc+tectonic into the sandbox image, probe at start,
  ship .tex (tectonic is not apt-installable on bookworm-slim)
- [ ] Source originals copied into the mounted notes tree (in-sandbox reproduction)
- [ ] (decision) native-LaTeX authoring + collaboration surface for academic math
```

---

## Decisions locked (brainstorm 2026-06-14)

The brainstorm refined the design above. Where this section differs from the design as
originally written, **this section wins**.

- **Fix #3 is pure propagation, not an enforced field.** The spine is propagating the raw
  brief + author direction + verbatim feedback into every deciding stage (write, reconcile,
  review, resolve, rewrite). This routes around the failure the structured channel admits to
  (*"one classification error … defeats the whole enforcement layer"*): even if extraction
  mis-files an imperative, every agent still sees the original words. `ArticlePlan.constraints`
  is kept as a **soft, derived checklist** for the reviewer — advisory, not the immutable
  `deliverables`-style contract. No draft-scan tool (pure judgment).
- **LaTeX: end-to-end, academic-only.** Writers emit `.tex`; reconcile/review/rewrite operate
  on `.tex`. GDoc working tabs hold raw `.tex`; a read-only Preview tab holds the rasterized
  compiled PDF, refreshed at checkpoints. This is what makes "reproduce, don't cite" possible
  at draft time — markdown has no proof environment, so citing is easier than reproducing.
- **Custom image: explicit devtools build.** inkwell ships a Dockerfile (pandoc + tectonic
  static binary + poppler-utils + primed tectonic cache); `lup-devtools dev build-sandbox-image`
  builds & tags it; runs reference the tag and fail fast if absent. (The preview tab forces
  this: rendering requires a real compile, so tectonic must be in the image.)
- **Source-in-sandbox: copy file-based originals** into `notes/artifacts/sources/`; skip
  URL/conversation sources (already captured as text).
- **Interrupt finalize: local file + GDoc.** Write `snapshot.output` to a local `.md/.tex`
  first (survives even when the GDoc write is what's interrupted), then best-effort Final-tab
  write; log the interrupt with stage context.
- **Validation:** targeted tests now; re-run session `b78c80527a464a38` from `plan` later.
- **Build order:** Fix #3 propagation + soft checklist → LaTeX surface (writers `.tex`,
  Preview tab, custom image, source-in-sandbox) → interrupt grace.
