# Inkwell — Implementation Plan

## What Exists

- [x] Package renamed from `lup_template` to `inkwell`, all checks green
- [x] Domain models: `ArticlePlan`, `ResearchCompilation`, `SectionDraft`, `ReviewFinding`, `WritingOutput`
- [x] System prompt with voice matching, Google Doc protocol, writing guidelines
- [x] 8 subagent definitions: planner, researcher, section_writer, coherence_editor, narrative_reviewer, fact_checker, style_reviewer, rewriter
- [x] Config with Google OAuth, Exa, FRED, Claude cookie, style corpus settings
- [x] Tool policy with Google Docs + extraction tool sets
- [x] Claude conversation extractor (fully implemented, ported from `kernel/extractors/`)
- [x] Google Docs MCP tools (8 tools, stubs with full schemas and descriptions)
- [x] Writing-specific reflection tool (voice assessment, section status, research gaps)
- [x] CLI: `inkwell write`, `inkwell style add/list`, `inkwell run`, `inkwell loop`
- [x] CLAUDE.md updated for inkwell
- [x] Upstream sync baselined

## What Remains

### 1. Google Docs API Integration

Wire the 8 Google Docs tool stubs (`src/inkwell/agent/tools/google_docs.py`) to the actual API.

**Dependencies:** `google-api-python-client`, `google-auth-oauthlib`

**Tools to implement:**
- [ ] `create_doc` — Create doc via Docs API, share via Drive API
- [ ] `create_tab` — Add a tab (Google Docs calls these "tabs" in the API as of 2024)
- [ ] `write_tab` — Replace tab content with markdown (convert to Docs formatting or insert as plain text)
- [ ] `read_tab` — Read tab content as plain text
- [ ] `insert_comment` — Add comment via Drive API comments endpoint, optionally anchored to text
- [ ] `read_comments` — List comments with replies via Drive API
- [ ] `update_overview` — Write to the first/overview tab
- [ ] `list_tabs` — List all tabs in a doc

**Decisions needed:**
- Markdown-to-Docs formatting: should we convert markdown to native Google Docs formatting, or insert as plain text? Plain text is simpler but looks worse. Native formatting requires building Docs API batch update requests.
- Comment anchoring: the Docs API anchoring model is complex (uses content ranges). Start with unanchored comments and add anchoring later?

### 2. Setup Wizard

Interactive setup command following the `assistant/` pattern (`src/inkwell/devtools/setup.py`).

- [ ] `inkwell setup` — Main wizard with status table
- [ ] `inkwell setup google` — Google OAuth flow (open browser, save credentials)
- [ ] `inkwell setup status` — Show what's configured vs missing
- [ ] `inkwell setup exa` — Prompt for Exa API key
- [ ] `inkwell setup claude` — Prompt for Claude cookie (with instructions for getting it)
- [ ] Status table with rich.Table showing configured integrations
- [ ] Validate tokens on entry (test Google API access, test Exa key)
- [ ] Save secrets to `.env.local`

### 3. Research Tools (from aib)

Port research tools from `/home/pfftz/job/onit/aib-joy-void-joy-bot.git/tree/main`. Each tool goes in `src/inkwell/agent/tools/`.

**Web research:**
- [ ] `exa_search` — Exa AI search with livecrawl, date filtering, domain filtering (from `aib/tools/exa.py`)
- [ ] `fetch_url` — HTTP fetch with trafilatura + Playwright fallback (from `aib/tools/fetch_http.py`, `aib/tools/search.py`)

**Academic:**
- [ ] `search_arxiv` — arXiv paper search (from `aib/tools/arxiv_search.py`)
- [ ] `fetch_wikipedia` — Wikipedia article with historical version support (from `aib/tools/search.py`)

**Prediction markets:**
- [ ] `polymarket_price` — Polymarket search + prices + history (from `aib/tools/markets.py`)
- [ ] `manifold_price` — Manifold Markets prices (from `aib/tools/markets.py`)
- [ ] `kalshi_event` — Kalshi bracket markets (from `aib/tools/markets.py`)
- [ ] `search_markets` — Unified cross-platform market search (from `aib/tools/markets.py`)

**Economic/financial:**
- [ ] `fred_series` / `fred_search` — FRED economic data (from `aib/tools/financial.py`)
- [ ] `yfinance_price` / `yfinance_historical` — Stock data (from `aib/tools/financial.py`)
- [ ] `bls_series` — Bureau of Labor Statistics (from `aib/tools/government.py`)

**Social/trends:**
- [ ] `reddit_search` — Reddit posts with sentiment (from `aib/tools/search.py`)
- [ ] `google_trends` — Search interest over time (from `aib/tools/search.py`)

**Infrastructure needed:**
- [ ] Rate limiting per API (port throttle instances from aib)
- [ ] Caching layer for research results (TTL-based, port from aib)
- [ ] MCP server factories for each tool group (research, markets, financial)
- [ ] Update `tool_policy.py` with new tool sets and API key gating

### 4. Persistent Agent Mode

Convert from one-shot pipeline to persistent sleep/wake agent that stays alive for revision passes.

- [ ] Wire `Scheduler` from `lup.realtime` into the session
- [ ] Add Stop hook (`create_stop_guard`) to prevent turn ending
- [ ] Implement sleep/context/reply tools from `agent/tools/realtime.py`
- [ ] Replace `run_agent()` with sleep/wake loop in `core.py`
- [ ] Gate `sleep` instead of `StructuredOutput` in reflection
- [ ] Poll for new Google Doc comments on wake
- [ ] Terminal input channel: accept commands during agent sleep (revision requests, direction changes)

### 5. Voice Analysis

Tools and logic for analyzing and matching the author's writing style.

- [ ] `analyze_voice` tool — Extract style characteristics from source conversation (sentence length distribution, formality, hedging patterns, humor, technical depth)
- [ ] Style corpus loader — Read reference pieces from `config/style/`, fetch URLs, cache extracted text
- [ ] Voice profile model — Pydantic model capturing extracted style characteristics
- [ ] Pass voice profile to section writers and rewriter via prompt context

### 6. Additional Extractors

Currently only Claude conversations are supported. Add more input sources.

- [ ] URL extractor — Generic URL → markdown via trafilatura (for `--ref` URLs)
- [ ] File extractor — Read local files (markdown, text, PDF)
- [ ] Google Doc extractor — Read an existing Google Doc as source material (for `--brief`)
- [ ] LessWrong extractor — Port from `kernel/extractors/lesswrong.py` (for reading existing LW posts as style references)
- [ ] Telegram extractor — Port from `kernel/extractors/telegram.py` (for Telegram conversation inputs)

### 7. Output Format Adapters

The pipeline produces a generic draft. Adapters transform it for specific publication formats.

- [ ] LessWrong adapter — Epistemic status header, footnotes, cross-references, appropriate heading structure
- [ ] Twitter thread adapter — Split into tweets, add hooks, ensure each tweet stands alone, thread numbering
- [ ] Blog adapter — SEO-friendly structure, meta description, featured image suggestion
- [ ] Format selection logic in the planner subagent (already has `target_format` field)

### 8. Testing

- [ ] Unit tests for Claude conversation extractor (mock HTTP responses)
- [ ] Unit tests for voice analysis
- [ ] Unit tests for output format adapters
- [ ] Integration test for Google Docs tools (requires credentials)
- [ ] Integration test for full pipeline (end-to-end with a sample conversation)
- [ ] Test fixtures: sample Claude conversations, style corpus files

### 9. Feedback Loop Customization

- [ ] Customize `devtools/feedback/` for writing-specific metrics:
  - Word count per session
  - Sections completed vs planned
  - Reviewer findings by severity
  - Research depth (sources per claim)
  - Voice consistency score (if measurable)
- [ ] Define what "ground truth" means for writing: author satisfaction? Publication acceptance? Reader engagement?
- [ ] Trace analysis patterns specific to writing: where does the pipeline produce weak sections?

## Priority Order

1. **Google Docs API** — Without this, the agent can't produce visible output
2. **Setup wizard** — Needed to configure Google OAuth before anything works
3. **Research tools** — The pipeline is only as good as its research
4. **Persistent mode** — Enables the revision workflow
5. **Voice analysis** — Improves output quality
6. **Additional extractors** — Expands input sources
7. **Output format adapters** — Polishes the final product
8. **Testing** — Build alongside each feature
9. **Feedback loop** — Iterate after running real sessions
