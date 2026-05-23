# Inkwell Design Document

## Overview

Inkwell transforms a Claude conversation (or other source material) into a polished, published article. The author pastes a share link, and the agent extracts a plan, researches every claim, writes each section in the author's voice, reviews the draft, and produces a final version — all visible in real time through a Google Doc.

The author follows along, leaves comments whenever they want, and the agent picks up feedback at natural checkpoints. No blocking, no terminal babysitting.

## Core Decisions

### Google Doc as the Live Surface

The Google Doc is the output, the collaboration channel, and the progress dashboard — not an intermediate format.

| Feature | Purpose |
|---|---|
| **Tabs** | Parallel section writing (one tab per section, no conflicts) |
| **Comments** | Async Q&A (agent asks questions, author replies whenever) |
| **Overview tab** | Progress dashboard the author checks at a glance |
| **Draft tab** | Merged draft where reviewers leave anchored comments |
| **Final tab** | Polished article after incorporating all feedback |

### Persistent Agent (Sleep/Wake)

The agent stays alive across the full session. After stages that produce visible output (section drafts, review comments), it sleeps and lets the author read. On wake, it checks for new author comments and decides whether to continue or adjust.

Two natural sleep points:
1. **After section drafts** — "Sections are in their tabs. Take a look, leave comments on anything."
2. **After review comments** — "Reviewers have annotated the draft. Weigh in before I write the final version."

The agent can also sleep at any point where it has a question it can't resolve on its own — it leaves a comment and sleeps until the author replies or a timer fires.

### Markdown → Native Docs Formatting

Write content as markdown internally, convert to Google Docs BatchUpdate requests on write. Supports:
- Headings (`#`, `##`, `###`) → `HEADING_1/2/3` paragraph styles
- **Bold**, *italic* → `updateTextStyle`
- `[text](url)` → linked text
- `- item` → bullet lists
- Everything else → plain text

Uses `markdown-it-py` to parse markdown to AST, walks the tree, emits BatchUpdate operations. ~150 lines. Skip tables and code blocks until needed.

### Reviewer Output → Google Doc Comments

Reviewers return structured `ReviewFinding` objects (for the rewriter to process programmatically). The orchestrator then converts each finding to a Google Doc comment anchored to the relevant text. This gives:

- The rewriter gets structured data it can work with
- The author sees findings as comments in real time
- The author can reply to findings, and the rewriter reads those replies before the final pass

Flow: reviewers (parallel) → `list[ReviewFinding]` per reviewer → orchestrator calls `insert_comment` for each finding with `anchor_text` → rewriter reads structured findings + author replies.

## Pipeline Stages

```
                    ┌──────────────────────────────────────────────┐
                    │              Google Doc                       │
                    │                                              │
  ┌─────────┐      │  ┌──────────┐  ┌──────┐  ┌──────┐          │
  │ Extract  │──────┤  │ Overview │  │ §1   │  │ §2   │  ...     │
  └────┬─────┘      │  │ (status) │  │ tab  │  │ tab  │          │
       │            │  └──────────┘  └──────┘  └──────┘          │
  ┌────▼─────┐      │                                              │
  │  Plan    │      │  ┌───────┐  ┌───────┐                       │
  └────┬─────┘      │  │ Draft │  │ Final │                       │
       │            │  │ tab   │  │ tab   │                       │
  ┌────▼─────┐      │  └───────┘  └───────┘                       │
  │ Research │      │                                              │
  └────┬─────┘      │  ┌─────────────────────┐                    │
       │            │  │ Comments             │                    │
  ┌────▼─────┐      │  │  - agent questions   │                    │
  │  Write   │──────┤  │  - author replies    │                    │
  │ (parallel│      │  │  - reviewer findings │                    │
  │  per tab)│      │  └─────────────────────┘                    │
  └────┬─────┘      │                                              │
       │            └──────────────────────────────────────────────┘
  ┌────▼─────┐
  │  sleep   │  ← author reads section tabs, leaves comments
  └────┬─────┘
       │
  ┌────▼─────┐
  │  Merge   │  → Draft tab
  └────┬─────┘
       │
  ┌────▼───────────────────┐
  │  Review (3× parallel)  │  → comments on Draft tab
  │  - narrative coherence │
  │  - fact-check          │
  │  - style/voice         │
  └────┬───────────────────┘
       │
  ┌────▼─────┐
  │  sleep   │  ← author reads review comments, replies
  └────┬─────┘
       │
  ┌────▼─────┐
  │ Rewrite  │  → Final tab
  └────┬─────┘
       │
  ┌────▼─────┐
  │ Reflect  │  → session metadata
  └──────────┘
```

### Stage Details

**1. Extract** — Parse the Claude share link into structured markdown with `<user>`/`<claude>` speaker tags. Also fetch any `--ref` URLs (trafilatura for web pages, local read for files).

**2. Plan** — Opus subagent extracts from the conversation:
- Working title and thesis
- Ordered section outline with key points per section
- Research questions (what needs verification or sourcing)
- Exact quotes from the user to preserve verbatim
- Author direction (constraints, preferences, explicit guidance)
- Voice notes (tone, rhythm, formality, humor)
- Target format (lesswrong, twitter, blog)

Output: `ArticlePlan` (structured, not prose).

At this point the agent creates the Google Doc, the Overview tab, and one tab per section.

**3. Research** — Opus subagent investigates each research question:
- Web search (Exa for semantic search, WebSearch for broad)
- Academic papers (arXiv search + full paper fetch)
- Prediction markets (Polymarket, Manifold — for epistemic articles)
- Economic data (FRED — for policy articles)
- Code execution (sandbox — for data analysis, charts)

Output: `ResearchCompilation` with findings, sources, confidence levels, data points.

**4. Write** — One section writer subagent per section, running in parallel. Each:
- Receives the section plan, relevant research, voice notes, quotes to weave in
- Writes into its own Google Doc tab
- Can do additional research if gaps emerge
- Leaves questions for the author as Google Doc comments (tagged `[QUESTION]`)

Output per writer: `SectionDraft` + content written to the tab.

**5. Sleep checkpoint 1** — Agent updates Overview tab with section status, then sleeps. Author reads the tabs, leaves comments. On wake, agent calls `check_author_feedback` and adjusts before merging.

**6. Merge** — Coherence editor reads all section tabs, merges into a single Draft tab. Fixes transitions, removes redundancy, normalizes depth.

**7. Review** — Three reviewers run in parallel, each reading the Draft tab:
- **Narrative reviewer** (Sonnet) — flow, hooks, argument progression
- **Fact checker** (Opus) — every claim verified against sources
- **Style reviewer** (Sonnet) — voice consistency, clarity, cliches

Each returns `list[ReviewFinding]`. Orchestrator converts findings to Google Doc comments anchored to specific text on the Draft tab.

**8. Sleep checkpoint 2** — Agent updates Overview, sleeps. Author reads review comments, replies to disagree or add context. On wake, agent reads author replies.

**9. Rewrite** — Final Opus subagent:
- Reads the Draft tab content
- Reads all structured `ReviewFinding` objects (prioritizing critical > suggestion)
- Reads author comment replies (author preferences override reviewer suggestions)
- Applies fixes section by section
- Writes the final version to the Final tab

**10. Reflect** — Standard reflection gate: assessment, confidence, voice evaluation, tool audit, process reflection. Reviewer sub-agent checks pipeline completeness.

## MCP Tool Architecture

### Tool Groups

**Google Docs (`docs` server)** — 8 tools + 3 new tools:

| Tool | Description |
|---|---|
| `create_doc` | Create doc, share with author |
| `create_tab` | Create a section tab |
| `write_tab` | Write markdown → native formatting to a tab |
| `read_tab` | Read tab content as text |
| `insert_comment` | Comment anchored to text (for questions + findings) |
| `read_comments` | List all comments with replies |
| `update_overview` | Refresh the progress dashboard tab |
| `list_tabs` | List available tabs |
| `ask_author` | Typed, tagged comment (question/direction_check/fact_verify) |
| `check_author_feedback` | Read unresolved comments where author has replied |
| `update_progress` | Structured progress update to Overview tab |

`ask_author` wraps `insert_comment` with semantics: tags the comment with a type, records it in session state, and the agent continues with its best guess. `check_author_feedback` is the agent's "check inbox" — reads comment threads where the author has replied.

`update_progress` is a structured write to the Overview tab, not freeform — it renders a progress table (sections drafted/reviewed/finalized, pending questions, next steps).

**Conversation Extraction (`extract` server)** — 1 tool:

| Tool | Description |
|---|---|
| `extract_conversation` | Claude share link → structured markdown |

Already implemented. Future: add extractors for generic URLs, local files, LessWrong posts, Telegram.

**Research (`research` server)** — 6-8 tools:

| Tool | Source | Priority |
|---|---|---|
| `exa_search` | Exa AI | P0 — primary semantic web search |
| `fetch_url` | trafilatura | P0 — clean text from any URL |
| `search_arxiv` | arXiv API | P0 — academic papers |
| `fetch_arxiv` | arXiv + trafilatura | P1 — full paper text |
| `search_markets` | Polymarket + Manifold | P1 — prediction market consensus |
| `polymarket_price` | Polymarket | P1 — specific market prices + history |
| `manifold_price` | Manifold | P1 — specific market prices |
| `fred_series` | FRED | P2 — economic time series |

These are ported from `/home/pfftz/job/onit/aib-joy-void-joy-bot.git/tree/main`. Each gets its own rate limiting and caching.

**Realtime (`realtime` server)** — adapted from the template:

| Tool | Description |
|---|---|
| `sleep` | Pause agent, schedule wake-up |
| `context` | Read session state + new events |
| `meta` | Process self-assessment (required before sleep) |
| `notes` | Private session notes |
| `remind` | Schedule a self-prompt |

The `reply` tool from the template becomes less central — the agent's primary output channel is the Google Doc, not terminal messages. `reply` is still useful for CLI status updates.

**Reflect (`notes` server)** — 1 tool:

| Tool | Description |
|---|---|
| `review` | Structured self-assessment before finalization |

Already implemented with writing-specific fields.

### Tool Availability by Subagent

| Subagent | Builtin Tools | MCP Tools |
|---|---|---|
| **Main orchestrator** | WebSearch, WebFetch, Read, Glob, Grep, Bash | All MCP servers |
| **Planner** | Read, Glob, WebFetch | extract |
| **Researcher** | WebSearch, WebFetch, Read, Glob, Grep, Bash | research, sandbox |
| **Section writer** | WebSearch, WebFetch, Read, Glob, Grep, Bash | research, docs (write_tab, insert_comment), sandbox |
| **Coherence editor** | Read, Glob | docs (read_tab, write_tab) |
| **Narrative reviewer** | WebSearch, WebFetch, Read, Glob, Grep | docs (read_tab, insert_comment) |
| **Fact checker** | WebSearch, WebFetch, Read, Glob, Grep | research, docs (read_tab, insert_comment) |
| **Style reviewer** | WebSearch, WebFetch, Read, Glob, Grep | docs (read_tab) |
| **Rewriter** | Read, Glob | docs (read_tab, write_tab, read_comments) |

## Persistence Model

### Session State

The `Scheduler` from `lup.realtime` manages the sleep/wake cycle. The agent's "environment" is the Google Doc — events are:

- Author comments (new or replied-to)
- Timer expiry (sleep duration reached)
- Reminders (self-prompts)

The `build_context` callback returns:

```python
class WritingContext(ContextOutput):
    stage: str                           # current pipeline stage
    doc_id: str                          # Google Doc ID
    doc_url: str                         # Google Doc URL
    sections_status: dict[str, str]      # section_title -> "planned"|"drafted"|"reviewed"|"final"
    pending_questions: list[str]         # unanswered questions for the author
    new_author_comments: list[str]       # unread author replies since last wake
    plan: ArticlePlan | None             # current plan
    research: ResearchCompilation | None # current research
```

### Sleep/Wake Protocol

1. Agent calls `meta` (process reflection — required by gate)
2. Agent calls `update_progress` (refresh Overview tab)
3. Agent calls `sleep(seconds=600)` (10 min default, adjustable)
4. On wake: agent calls `context` to read state
5. Agent calls `check_author_feedback` to read new comments
6. Agent decides: continue pipeline or address feedback first

The Stop hook (`create_stop_guard`) prevents the agent from ending its turn while the pipeline is incomplete. The agent can only produce `StructuredOutput` after calling `review` (reflection gate).

### Terminal Input

During sleep, the terminal accepts commands:
- Direct text → becomes an event that wakes the agent (revision request, direction change)
- `status` → prints current pipeline stage and section status
- `Ctrl+C` → graceful shutdown, saves state for resume

## Setup Wizard

Interactive Typer CLI following the rlaif pattern:

```
inkwell setup           # Full walkthrough
inkwell setup google    # Google OAuth flow
inkwell setup exa       # Exa API key
inkwell setup status    # Configuration status table
```

### Google OAuth Flow

1. Guide user through Google Cloud Console:
   - Create project
   - Enable Google Docs API + Google Drive API
   - Create OAuth consent screen (External, add self as test user)
   - Create Desktop OAuth client ID
   - Download credentials JSON
2. User provides path to downloaded credentials JSON
3. Copy to `credentials/google.json`
4. Run browser-based OAuth flow (`run_local_server(port=0)`)
5. Save token to `credentials/token.json`
6. Store paths in `.env.local`

Scopes needed:
- `https://www.googleapis.com/auth/documents` (Docs read/write)
- `https://www.googleapis.com/auth/drive.file` (Drive: files created by the app)

Runtime: `ServiceFactory` creates fresh Docs/Drive service objects per call, auto-refreshes expired tokens.

### Exa Setup

1. Prompt for API key
2. Validate with a test search
3. Store in `.env.local`

### Status Display

Rich table showing:
- Google: `OK` / `credentials present (awaiting OAuth)` / `not configured`
- Exa: `OK` / `not configured`
- Claude cookie: `OK` / `not configured`
- FRED: `OK` / `not configured` (optional)

## Voice Analysis

### Style Corpus

The author builds a corpus of reference writing via `inkwell style add`:
- URLs (fetched with trafilatura, cached as markdown)
- Local files (copied to `config/style/`)

### Voice Profile

The planner extracts voice characteristics from the source conversation:
- Sentence length distribution (short/medium/long mix)
- Formality level
- Hedging patterns ("I think", "probably", "it seems")
- Humor style (if any)
- Technical depth vs. accessibility
- Characteristic phrases

This becomes a `VoiceProfile` Pydantic model passed to section writers and the rewriter.

### Voice Matching in Section Writers

Each section writer receives:
- The `VoiceProfile` extracted from the conversation
- 2-3 style corpus samples (if available)
- Instructions to match tone, not just content

The style reviewer specifically checks for voice drift — sections that sound like "generic AI prose" rather than the author.

## Output Format Adapters

The pipeline produces a format-neutral draft. Adapters transform for publication:

**LessWrong:**
- Epistemic status header ("Epistemic status: fairly confident, based on...")
- Footnotes for asides
- Cross-references to related posts
- Appropriate heading depth

**Twitter thread:**
- Split into tweets (280 chars, accounting for thread numbering)
- Hook in tweet 1
- Each tweet stands alone but builds on the thread
- Thread numbering ("1/N")

**Blog:**
- SEO-friendly structure
- Meta description
- Featured image suggestion
- Subheading density for scannability

The `target_format` field in `ArticlePlan` selects the adapter. The planner considers the format when structuring the outline (e.g., Twitter threads need fewer, punchier sections).

## Research Tool Details

### Exa Search (from aib)

Primary semantic web search. Better than raw WebSearch for research because:
- Livecrawl mode gets fresh content
- Domain filtering (include/exclude specific sites)
- Date range filtering
- Highlights extraction for key passages

```python
class ExaSearchInput(BaseModel):
    query: str = Field(description="Search query")
    num_results: int = Field(default=10, ge=1, le=30)
    livecrawl: str = Field(default="fallback", description="'always'|'fallback'|'never'")
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    published_after: str | None = Field(default=None, description="ISO date")
    published_before: str | None = Field(default=None, description="ISO date")
```

### arXiv Search (from aib)

Academic paper search using the arXiv API.

```python
class ArxivSearchInput(BaseModel):
    query: str = Field(description="arXiv query (supports au:, ti:, cat: syntax)")
    max_results: int = Field(default=10, ge=1, le=50)
```

Returns: paper ID, title, abstract, authors, published date, PDF link.

`fetch_arxiv` retrieves the full paper text (HTML preferred, PDF fallback via trafilatura).

### Prediction Markets (from aib)

Unified search across Polymarket and Manifold. For epistemic articles, prediction market consensus is a unique signal — "what do informed bettors think about X?"

```python
class MarketSearchInput(BaseModel):
    query: str = Field(description="What to search for across prediction markets")

class MarketPriceInput(BaseModel):
    market_id: str = Field(description="Platform-specific market ID")
    include_history: bool = Field(default=False)
    history_days: int = Field(default=7)
```

### FRED Economic Data (from aib)

Federal Reserve Economic Data — 500k+ US economic time series.

```python
class FredSeriesInput(BaseModel):
    series_id: str = Field(description="FRED series ID (e.g., 'UNRATE', 'CPIAUCSL')")
    start_date: str | None = Field(default=None)
    end_date: str | None = Field(default=None)
```

### URL Fetch (from aib)

Clean text extraction from any URL using trafilatura. Much better than raw HTTP for extracting readable article text.

```python
class FetchUrlInput(BaseModel):
    url: str = Field(description="URL to fetch and extract text from")
```

## File Structure (New/Modified)

```
src/inkwell/
├── agent/
│   ├── tools/
│   │   ├── google_docs.py      # Wire stubs to real API + markdown converter
│   │   ├── author.py           # ask_author, check_author_feedback, update_progress
│   │   ├── research/
│   │   │   ├── __init__.py
│   │   │   ├── exa.py          # Exa search
│   │   │   ├── arxiv.py        # arXiv search + fetch
│   │   │   ├── markets.py      # Prediction market tools
│   │   │   ├── fred.py         # FRED economic data
│   │   │   └── fetch.py        # URL fetch (trafilatura)
│   │   ├── extract.py          # (exists) Claude conversation extractor
│   │   ├── reflect.py          # (exists) Writing reflection tool
│   │   └── realtime.py         # (exists, adapt) Sleep/wake tools
│   ├── core.py                 # (modify) Add persistence, sleep/wake loop
│   ├── config.py               # (modify) Add research API key settings
│   ├── models.py               # (modify) Add VoiceProfile, WritingContext
│   ├── subagents.py            # (modify) Update tool lists for new MCP servers
│   ├── tool_policy.py          # (modify) Gate research tools on API keys
│   ├── prompts.py              # (exists, minor updates)
│   ├── markdown_to_docs.py     # NEW: markdown AST → BatchUpdate requests
│   └── google_auth.py          # NEW: OAuth flow, token refresh, ServiceFactory
├── devtools/
│   ├── setup.py                # NEW: Setup wizard (google, exa, status)
│   └── ...
└── environment/
    └── cli/
        └── __main__.py         # (modify) Add terminal input during sleep
```

## Implementation Order

1. **Google OAuth + Setup Wizard** — Can't test anything without credentials
2. **Google Docs API** — Wire stubs, markdown converter. Core output surface.
3. **Persistence (sleep/wake)** — Convert one-shot to persistent. Wire Scheduler.
4. **Author interaction tools** — ask_author, check_author_feedback, update_progress
5. **Research tools** — Port exa, arxiv, markets, fred, fetch from aib
6. **Voice analysis** — VoiceProfile model, style corpus loading
7. **Output format adapters** — LessWrong, Twitter, blog
8. **Testing** — Build alongside each feature

Steps 1-4 make the agent functional (can write articles with basic WebSearch). Steps 5-7 make it good.

## Dependencies to Add

```
google-api-python-client    # Google Docs + Drive API
google-auth-oauthlib        # OAuth flow
google-auth-httplib2        # HTTP transport for Google API
markdown-it-py              # Markdown parsing to AST
exa-py                      # Exa search client
trafilatura                 # URL text extraction
arxiv                       # arXiv API client
```
