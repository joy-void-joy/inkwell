# Inkwell — Implementation Plan

## What Exists

- [x] Package renamed from `lup_template` to `inkwell`, all checks green
- [x] Domain models: `ArticlePlan`, `ResearchCompilation`, `SectionDraft`, `ReviewFinding`, `WritingOutput`
- [x] System prompt with voice matching, Google Doc protocol, writing guidelines
- [x] 8 subagent definitions: planner, researcher, section_writer, coherence_editor, narrative_reviewer, fact_checker, style_reviewer, rewriter
- [x] Config with Google OAuth, Exa, FRED, Claude cookie, style corpus settings
- [x] Tool policy with Google Docs + extraction + research tool sets, API key gating
- [x] Claude conversation extractor (fully implemented)
- [x] URL extractor (trafilatura-based web page text extraction)
- [x] File extractor (local markdown/text files)
- [x] Google Docs MCP tools (8 tools, fully implemented with real API calls)
- [x] Markdown-to-native-Docs converter (`markdown_to_docs.py`, headings/bold/italic/links/lists)
- [x] Google OAuth flow + ServiceFactory (`google_auth.py`)
- [x] Author interaction tools: `ask_author`, `check_author_feedback`, `update_progress`
- [x] Writing-specific reflection tool (voice assessment, section status, research gaps)
- [x] Research tools: Exa search, arXiv search/fetch, URL fetch
- [x] Research tools: FRED search/series (economic data)
- [x] Research tools: Polymarket, Manifold, cross-platform market search
- [x] Research tools: Wikipedia search/fetch with section extraction
- [x] Voice analysis: `VoiceProfile` model, style corpus loader (`load_corpus` tool)
- [x] Format adapters: LessWrong (epistemic status, footnotes), Twitter thread, blog (SEO)
- [x] Persistent agent mode: `WritingSessionState`, `Scheduler`, stop/meta/event guards
- [x] Live GDoc state in `build_context`: sections, comments, terminal messages
- [x] `check_unread` wired to real Drive API comment polling
- [x] CLI: `inkwell write`, `inkwell style add/list`, `inkwell run`, `inkwell setup`
- [x] Setup wizard: Google OAuth, Exa, Claude cookie, FRED, status table
- [x] `on_action` callback wired from CLI for terminal output
- [x] CLAUDE.md updated for inkwell
- [x] Upstream sync baselined
- [x] Unified pipeline: batch and interactive share `run_pipeline()` via `PipelineListener`
- [x] Voice analysis integrated into pipeline (structured `VoiceProfile` passed to writers)
- [x] Format adapters wired into pipeline (auto-applied after rewrite stage)
- [x] Author feedback collected at every stage boundary (Google Doc comments + terminal input)
- [x] Stage prompts consolidated in `stages.py` (renamed from `agents.py`)
- [x] `do_*` shared functions: Google Docs, voice analysis, source extraction (pipeline + MCP tools share code)
- [x] Single source of truth for tool lists (`research_tool_names()`, `review_tool_names()` in tool_policy.py)
- [x] `max_budget_usd` wired through all pipeline stages and `run_batch()`
- [x] Post-pipeline revision loop via `PipelineListener.collect_revision()`
- [x] Dead code removed (duplicate prompts, shadow PipelineError, unused vars, double ToolPolicy)

## What Remains

### 0. Source-Fidelity Overhaul (decisions from session 1b4114eeabf24695 review)

Trace review + author interview decisions. Work lands directly on `dev` (explicit override of the worktree rule for this effort).

**Phase A — wiring bugfixes** (existing capabilities, stranded):

- [x] `apply_format`: dispatch `academic` → `do_format_academic` (today falls through to identity while events log "Applying academic formatting")
- [x] Feedback round-trip: agent-authored GDoc comments re-ingested as `[GDoc Comment]` author feedback — tag via `agent_comment_ids`, never present pipeline output as author input
- [x] Cost accumulator: every `cost_update` was 0.0 for the full session
- [x] Sandbox mounts: `execute_code` cannot see `pipeline_notes/artifacts` (researcher hit FileNotFoundError, retyped data from memory)
- [x] `fetch_and_extract`: detect anti-bot/challenge pages (Anubis page was accepted as content)
- [x] Trace markdown: emit stage-boundary markers (stage attribution had to be reverse-engineered)

**Phase B — source as a first-class citizen:**

- [x] Brief vs sources split: preprocess emits an immutable Brief (instructions, author comments, deliverable criteria) separate from `sources[]`; the planner plans the brief's deliverable using sources as reference — not "distill the source"
- [x] Deliverable contract: plan carries explicit criteria from the brief (e.g. self-contained proofs, single logic) that reviewers check the draft against
- [x] Scope gate: brief is immutable; refiner may record proposed deviations but the pipeline proceeds with the author's stated scope unless approved
- [x] `consult_source(question, pages?)` nested-reader tool: subagent reads the actual PDF (visual Read — no text-extraction-as-content), returns answers with page refs; available to every stage
- [x] `find_in_source(pattern)` locator: greps a throwaway text layer, returns page numbers only (navigation, never content)
- [x] Source listed in every stage manifest (write, merge, review, rewrite)
- [x] `ResearchFinding` provenance: origin discriminator — `source_document` (requires verbatim quote + page locator) vs `external` (URL); unquoted source claims are unverified

**Phase C — pipeline shape + models:**

- [x] Per-stage model config (stage→model map in settings; `claude-fable-5` usable)
- [x] Single-writer mode as an orthogonal pipeline option (not tied to model): one writer drafts the whole piece with `consult_source` + research tools; no merge stage
- [x] Compaction resilience: reading pass via nested readers writing per-chapter notes (verbatim key passages + page refs) to disk; no stage depends on holding the whole source in context

**Phase D — review + coherence:**

- [x] New source-fidelity reviewer (4th reviewer): verifies definitions, theorem statements, and proof structure against cited source pages via `consult_source`
- [x] Fact-checker: keep web + compute duties; recompute examples from the source's definitions (not the draft's premises); port aib REPL improvements (persistent session, `install_package`)
- [x] Resolve-against-source stage before rewrite: triage pending questions — source-answerable ones get answered from the source; only author-judgment questions reach the author
- [x] Generic `conventions` field on `ArticlePlan` dispatched to all writers (shared terms/concepts/conventions; domain-agnostic)
- [x] Merge plan: terminology/notation-consistency analysis dimension (generic, alongside duplication/transitions)
- [x] Severity split: correctness-critical vs style-critical; rewriter hierarchy puts correctness above voice rules
- [x] `FORMAT_GUIDANCE` `academic` entry (define before use, self-containment, notation conventions)

**Phase E — academic deliverable:**

- [x] LaTeX-first output for academic format: produce `paper.tex`, compile to PDF in the sandbox (tectonic), upload PDF to Drive and link it from the GDoc; GDoc remains the comment/review surface

### 1. ~~Terminal Input During Sleep~~ Interactive Chat CLI

- [x] Interactive chat as default (`inkwell` opens chat, subcommands pre-seed it)
- [x] Concurrent stdin + agent response collection (`collect_with_stdin()`)
- [x] Terminal input during sleep wakes scheduler
- [x] Terminal input during thinking surfaced via PreToolUse hook
- [x] In-chat slash commands: `/status`, `/doc`, `/style add|list`, `/help`, `/quit`
- [x] Ctrl+C interrupts agent, double-tap exits

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
- [x] Unit tests for format adapters (LessWrong, Twitter, blog)
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

0. **Source-fidelity overhaul** — Phases A→E in order; A is independent quick wins, B unblocks C/D/E
1. **Terminal input during sleep** — Enables the interactive revision workflow
2. **Testing** — Build alongside each feature
3. **Additional extractors** — Expands input sources
4. **Additional research tools** — Nice-to-have breadth
5. **Feedback loop** — Iterate after running real sessions
