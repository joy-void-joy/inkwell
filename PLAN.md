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
- [x] Notes tool wired to session storage (read/write/list/delete)
- [x] CLI: `inkwell write`, `inkwell style add/list`, `inkwell run`, `inkwell loop`, `inkwell setup`
- [x] Setup wizard: Google OAuth, Exa, Claude cookie, FRED, status table
- [x] `on_action` callback wired from CLI for terminal output
- [x] CLAUDE.md updated for inkwell
- [x] Upstream sync baselined

## What Remains

### 1. Terminal Input During Sleep

The sleep/wake infrastructure is fully wired (Scheduler, guards, context), but the CLI
doesn't yet accept terminal commands while the agent sleeps. `asyncio.run(run_session(...))`
blocks — needs an event loop that feeds user input as scheduler events.

- [ ] Add stdin reader in the CLI event loop during `query()` execution
- [ ] Feed typed text to `session_state.add_terminal_message()` + `scheduler.wake("terminal")`
- [ ] Handle `status` command (print section status without waking)
- [ ] Handle `Ctrl+C` gracefully (save state for resume)

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
- [ ] Unit tests for format adapters (LessWrong, Twitter, blog)
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

1. **Terminal input during sleep** — Enables the interactive revision workflow
2. **Testing** — Build alongside each feature
3. **Additional extractors** — Expands input sources
4. **Additional research tools** — Nice-to-have breadth
5. **Feedback loop** — Iterate after running real sessions
