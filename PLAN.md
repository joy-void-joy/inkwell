# Inkwell — Implementation Plan

## Current state

Inkwell runs the full pipeline (preprocess → extract → voice → plan → assumptions →
research → refine → write → reconcile → review → resolve → rewrite → format), with batch
and interactive sharing one path (`run_pipeline()` via `PipelineListener`). Three efforts
have shipped:

- **Source-fidelity** (session `1b4114eeabf24695`): immutable Brief vs `sources[]`; the
  `deliverables` contract reviewers enforce; `consult_source` / `find_in_source`
  nested-reader tools on every stage; source-fidelity + fact-check reviewers;
  resolve-against-source stage; `ResearchFinding` provenance discriminator; per-stage
  model config; single-writer mode; Docker sandbox with `/notes` mounted; academic LaTeX
  output (pandoc + tectonic).
- **Voice-fidelity** (sessions `3c7edec171014505` / `ca74e7f7c5594103`):
  `author_unverified` provenance; coverage reviewer; format-guidance-defers-to-voice
  precedence; author directions routed to reconcile / reviewers / rewriter; shared
  cross-writer glossary; voice-safe `reconcile` pass replacing the two-phase merge;
  `writer_mode: auto`.
- **Interactive chat CLI**: chat as default; concurrent stdin + agent response; in-chat
  slash commands; Ctrl+C interrupt. Research tools: Exa, arXiv, FRED, Polymarket/Manifold,
  Wikipedia. Formats: academic, LessWrong, Twitter, blog, memo, dialog.

## What Remains

### 0. Author-constraint enforcement (session `b78c80527a464a38`)

Asked to rewrite the author's **own** PhD thesis as an independent, simplified arXiv
paper, the pipeline instead cited the thesis (`[Mil16]`) 29× and delegated every proof
back to it — plus reused the private abbreviations (TOP 342×, QUAD 57×) and repeated the
dim-1 trivia, the exact defects the author flagged. Root cause: author directives are
consumed once at planning and are **never the evidence base for any downstream decision** —
they appear 0× across the entire write/review/rewrite trace. The writer reasoned from a
vacuum and defaulted to the academic norm (you *do* cite your own thesis); "cite vs.
reproduce" was re-litigated and failed open at every stage.

Full diagnosis: `notes/diagnoses/2026-06-14-author-directives-dont-bind.md`.
Fix design + locked decisions: `notes/diagnoses/2026-06-14-fix-design.md`.

The fix makes the author's direction **present everywhere a decision is made**
(propagation), not a new rule or gate. Authority lives in the raw direction; a
planner-derived soft checklist makes the reviewer systematic without becoming a brittle
single point of failure.

**Fix #3 — directives bind (propagation spine + soft checklist)**

- [x] Propagate the immutable brief + author direction + verbatim feedback into every
  deciding stage's context — `write` (`pipeline.py:1209`), `reconcile`, `review`, `resolve`
  (`pipeline.py:3842`), `rewrite` — not plan-only (today `collect_preexisting_directions`,
  `pipeline.py:2992`, injects them into `plan` alone).
- [x] Soft `ArticlePlan.constraints: list[str]` (`models.py:52`) — a *derived, advisory*
  checklist the planner distills from the direction; **not** the immutable `deliverables`
  contract. The reviewer's systematic checklist; authority stays in the propagated direction,
  so a misclassified item is backstopped rather than lost.
- [x] Reviewer (`stages.py:229`) checks the draft against the propagated direction + the
  checklist; a clear contradiction of an explicit direction is a serious finding routing
  `resolve → rewrite`.
- [x] `stage_resolve`: the direction is *privileged evidence* (outranks the source) for
  directive-questions, so "cite vs. reproduce" is pre-answered, never escalated; conservative
  default when genuinely open (no fail-open).

**LaTeX surface — end-to-end, academic-only (capability complement)**

- [x] Writers emit `.tex` fragments; `reconcile` assembles `.tex`; `review`/`rewrite` operate
  on `.tex`. Other formats stay markdown. Gives the writer a proof environment at draft time
  so "reproduce, don't cite" is possible.
- [x] GDoc working tabs host raw `.tex`; a read-only **Preview** tab hosts the rasterized
  compiled PDF (`InsertImage` from `/shared/`), refreshed at checkpoints. `stage_format`
  (`pipeline.py:3949`) rewritten for academic. Deliverable: `paper.tex` (+ PDF).

**Fix #1a — academic TeX toolchain (custom image, explicit build)**

- [x] inkwell ships a Dockerfile (uv base + `apt` pandoc/poppler-utils + `tectonic` static
  binary + primed tectonic cache). `lup-devtools dev build-sandbox-image` builds & tags it,
  but `ensure_sandbox_image` also builds it on demand if missing — at academic `start_sandbox`
  and at web-server startup (`lifespan`). `ensure_tex_tooling` (`latex.py:36`) → pure probe. (`apt install tectonic`
  can never work — not an apt package on `bookworm-slim`.)

**Fix #2 — source reachable in sandbox**

- [x] Copy file-based source originals → `notes/artifacts/sources/` at extract time (auto-
  reachable at `/notes/artifacts/sources/` via the existing ro mount, `pipeline.py:2408`);
  name the path in compute `usage_notes` (`pipeline.py:717`). The capability half of
  reproduce-not-cite. Skip URL/conversation sources (already captured as text).

**Fix #1b — interrupt grace + finalize**

- [x] Extract `is_interrupt(exc)` into `lup` (from `background.py:215`), shared with the main
  client. Catch around the stage loop (`pipeline.py:2689`); finalize writes `snapshot.output`
  to a local `.md/.tex` first, then best-effort Final-tab write; log the interrupt with stage
  context. Verify `write_with_continuation` reuses the `Final` tab → idempotent resume.
  *(Supersedes the deferred "actionable failure logging" item.)*

**Validation:** targeted tests (direction reaches each deciding stage; reviewer flags a draft
contradicting an explicit direction); re-run session `b78c80527a464a38` from `plan` as the
end-to-end proof.

**Build order:** Fix #3 propagation + soft checklist → LaTeX surface (writers `.tex`, Preview
tab, custom image, source-in-sandbox) → interrupt grace.

### 1. Deferred hardening

- [ ] Oversized output Doc: the single Doc carries every tab + 100+ comments and
  overwhelmed an external fetch; consider a publish/export path or splitting working tabs
  from the deliverable.

### 2. Additional Extractors

- [ ] Google Doc extractor — Read an existing Google Doc as source material (for `--brief`)
- [ ] LessWrong extractor — Port from `kernel/extractors/lesswrong.py` (style references)
- [ ] Telegram extractor — Port from `kernel/extractors/telegram.py` (conversation inputs)

### 3. Additional Research Tools

Lower-priority research tools not yet ported:

- [ ] `yfinance_price` / `yfinance_historical` — Stock data
- [ ] `bls_series` — Bureau of Labor Statistics
- [ ] `reddit_search` — Reddit posts with sentiment
- [ ] `google_trends` — Search interest over time

### 4. Testing

- [ ] Unit tests for Claude conversation extractor (mock HTTP responses)
- [ ] Unit tests for URL/file extractors
- [ ] Unit tests for research tools (mock API responses)
- [ ] Integration test for Google Docs tools (requires credentials)
- [ ] Integration test for full pipeline (end-to-end with a sample conversation)
- [ ] Test fixtures: sample Claude conversations, style corpus files

### 5. Feedback Loop Customization

- [ ] Customize `devtools/feedback/` for writing-specific metrics:
  - Word count per session
  - Sections completed vs planned
  - Reviewer findings by severity
  - Research depth (sources per claim)
  - Voice consistency score (if measurable)
- [ ] Define what "ground truth" means for writing: author satisfaction? Publication acceptance?
- [ ] Trace analysis patterns specific to writing: where does the pipeline produce weak sections?

## Priority Order

0. **Author-constraint enforcement** — Fix #3 (constraints channel) first; then
   interrupt grace + finalize; then academic TeX + source-in-sandbox.
1. **Testing** — Build alongside each feature
2. **Additional extractors** — Expands input sources
3. **Additional research tools** — Nice-to-have breadth
4. **Feedback loop** — Iterate after running real sessions
