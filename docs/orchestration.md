<!-- Generated from lup.devtools.harness.content.docs.orchestration by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Agent Orchestration Patterns

How work is delegated across agents in this project — what runs where, and who sees it. The recurring *code* shapes live in [docs/patterns.md](patterns.md); daily development guidance is in the agent guidance document your runtime loads.

**Model selection:** every pattern below — subagents, reviewers, nested and background agents — defaults to Opus 5 (`claude-opus-5`) or Fable (`claude-fable-5`). Drop to a cheaper model only with an explicit, justified reason (see § Model Selection in the agent guidance).

**Vocabulary:** two kinds of delegated agents look alike and must not be conflated:

- A **native subagent** ("subagent" for short) is dispatched by the harness itself: Claude Code's `Agent`/`Task` tool hands a focused task to a named role defined upfront, inside the main agent's session — shared trace, shared metrics. See [Subagent Pattern](#subagent-pattern).
- A **nested agent** (also called a *tool-subagent*) runs inside a tool call: the handler opens one independent session via `query()` with an explicit `SessionFactory` and folds the result into the tool's response. The harness never sees it — to the calling agent it is just a tool. See [Nested Agent Pattern](#nested-agent-pattern).

Guidance that says "subagent" unqualified means the native kind; an agent living inside a tool handler is always a nested agent.

---

## Persistent Agent Pattern

For agents that exist over time — maintaining conversations, monitoring systems, playing games — the architecture inverts: the agent is a **persistent presence** that controls its own attention, not a processor steered by an event queue.

| Do This                                                     | Not This                                    |
| ----------------------------------------------------------- | ------------------------------------------- |
| Agent sleeps when it chooses, wakes on events               | Event queue drives agent responses          |
| All timing is tools (sleep, debounce, remind, schedule)     | Hardcode delays or polling in orchestration |
| Stop hook prevents turn from ending — only sleep yields     | Request-response per event                  |
| Pull-based state reading (agent calls `context` when ready) | Push state changes as SDK user turns        |
| Agent parks thoughts (ideas, reminders) for later           | Drop context between interactions           |
| Expose environment state as tool-readable data              | Hide activity from the agent                |

**The core loop:** The agent never ends its turn. A Stop hook blocks it. Instead it cycles: wake → read context → think → act → meta-assess → sleep. The only way to yield control is `sleep()`, which blocks on an asyncio Event until something wakes it.

**Why not an event queue?** The sleep/wake pattern lets the agent stay centered — it can debounce event bursts, schedule actions, set reminders, and park thoughts for later, all on its own terms.

**Two wirings, one pattern:** On Claude, tools run in-process — `sleep` blocks on the Scheduler directly and a Stop hook keeps the single turn open forever. On backends whose tools run in a subprocess (Codex, OpenAI-compatible), the loop inverts: **each wake is one SDK turn**. The served tools (`lup.realtime.relay`) relay through files in `session_dir/realtime/`: `reply` and the timing tools append events a parent-side watcher applies mid-turn, `sleep` records a request and the agent ends its turn, and the parent loop (`run_relay_session`) consumes the request, sleeps on the Scheduler, and opens the next turn with a wake message. An agent that ends a turn without sleeping gets bounded corrective turns — the relay counterpart of the Stop hook. Enforcement is in-handler on both wirings (meta-before-sleep, unread-events guard), so no hooks are required.

**Library support:** `packages/lup/src/lup/realtime/scheduler.py` provides the `Scheduler` class and hook factories (`create_stop_guard`, `create_pending_event_guard`); `packages/lup/src/lup/realtime/relay.py` provides the subprocess wiring (`RealtimeMailbox`, `create_realtime_relay_tools`, `run_relay_session`), sharing the tool I/O models in `packages/lup/src/lup/realtime/models.py`. Construction goes through the engine seam: `run_persistent_agent` in `agent/core.py` serves the `session` tool group into the subprocess engine, builds the parent-side session mailbox itself, and drives it via `run_relay_session` — the engine never touches the relay. See example tools in `src/lup_template/agent/tools/realtime.py`.

---

## Reflection Pattern

Agents produce better output when forced to self-assess before committing. Three components:

1. **Reflection tool** (`agent/tools/reflect.py`): Domain-customizable self-assessment — confidence, uncertainties, tool audit, process reflection. Runs a nested reviewer agent that returns a structured `ReviewResult` verdict (skippable per call; a skip or reviewer failure records an approval so availability never deadlocks).
2. **Review gate** (`lup.reflect`): `ReviewGate`, a verdict-aware `ReflectionGate` — in-memory, or file-backed (fail counter included) when tools run in a subprocess. Approve and warn open the gate; fail keeps it closed so the agent revises and re-reviews; after 3 consecutive fails it opens anyway (escape hatch). Enforced primarily *inside* the `submit_output` handler (`lup.runtime.output`), which rejects submission with a retriable error until the gate opens; `create_reflection_gate()` adds a PreToolUse hook as hardening where the backend supports it. The plain `ReflectionGate` base remains for act-of-reflecting gates (the realtime `sleep` meta-gate).
3. **Wiring**: The gate rides inside submission — `reflection_submission_gate` (`agent/core.py`) adapts the `ReviewGate` to the `SubmissionGate` carried by the turn's `TurnToolBinding`, so a gated submission is rejected with a retriable message until the reviewer passes (persistent agents gate `sleep` instead). Final output always flows through the turn-bound submission tool — registered by the adapter on every SDK backend — whose `submit_output` handler validates against the turn's output model and persists through the bound `SubmittedOutputStore` (in-memory, or file-backed when tools run in a subprocess). Completion is enforced by the logical turn itself: `ResilientTurn` sends bounded corrective cycles (`CorrectionConfig`, `lup.runtime.wrappers`) when a turn ends without a submission, and a turn that still produces none raises `StructuredOutputError` for the orchestration layer — the one-shot counterpart of the relay's missing-sleep message. `create_completion_guard` (`lup.hooks`) remains as optional Stop-hook hardening on backends that expose stop hooks.

**Customizing:** The gate in `lup.reflect` is domain-neutral and parametric. The reflection tool and `ReflectInput` in `agent/tools/reflect.py` are domain-specific — add fields for your domain. The reviewer prompt should target your domain's common failure modes.

**Skip reviewer:** Set `skip_reviewer=True` for speed-sensitive or trivial tasks. The reviewer adds latency but catches calibration errors and reasoning gaps.

**Tool gates (generalization):** The reflection gate is one instance of a general primitive: `create_tool_gate()` in `lup.hooks` denies a tool (or Stop) with an agent-readable message until a condition unlocks it — "the agent must do A before it may do B" as a structural constraint instead of a prompt rule. Presets built on it: `create_reflection_gate` (reflect before finalizing output), `create_stop_guard` (sleep instead of ending the turn), `create_pending_event_guard` (read events before timing tools), and `create_meta_before_sleep_guard` (meta-assess before sleep). Reach for the primitive directly when your domain needs a new ordering constraint.

---

## Subagent Pattern

SDK-native delegation: the main agent dispatches a focused task to a named role defined upfront, sharing the session's trace and metrics. A **native subagent** extends the main agent's thinking — a specialized lobe with its own prompt, tool subset, and model — where a nested agent (below) isolates work in a separate context.

**Library support:** portable harness agents come from the typed harness catalog.
Application-time delegation uses `create_run_subagent_tool()` only with an
explicit `SubagentSpec -> SessionFactory` recipe; it never infers a provider or
reconstructs a native client.

---

## Nested Agent Pattern

Distinct from **native subagents** (defined upfront and delegated by the
harness). A **nested agent** — a *tool-subagent* — is a tool that receives or
builds an explicit independent `SessionFactory`, runs one typed query, and
folds the result into its response.

| Aspect     | Native Subagent                   | Nested Agent                        |
| ---------- | --------------------------------- | ----------------------------------- |
| Definition | Upfront in `get_subagent_specs()` | On-demand inside a tool handler     |
| Runtime    | Main agent's session               | Independent factory via `query()`   |
| Session    | Shared — same trace, same metrics | Isolated — no session persistence   |
| Return     | SDK `ResultMessage` (structured)  | Scalar result augmented by the tool |
| Use case   | Specialized long-running work     | Quick generation, review, parsing   |

**The augmentation pattern:** The tool handler post-processes the nested agent's output before returning it. The nested agent produces raw material; the tool shapes it into the MCP response:

```python
@lup_tool("Review code quality and return structured assessment")
async def review(params: ReviewInput) -> ReviewOutput:
    result = await query(review_factory, build_review_prompt(params), ReviewResult)
    return ReviewOutput(critique=result.output.assessment)
```

**Library support:** `lup.runtime.query.query(factory, prompt, OutputModel)`
opens one configured session and returns a strict `TurnResult[T]`; it also
accepts a `TurnInput` or a prepared `turn_request(...)` in place of the prompt. Provider selection,
tools, limits, and compatible endpoints are validated when the application
constructs the factory; unsupported settings are never silently dropped.

**Example:** `src/lup_template/agent/tools/nested.py` is the dedicated copyable template — a minimal `critique` tool (input model → `query()` → augmented output, exported as `NESTED_TOOLS`, unwired by default). For organic usages, see the reviewer inside `agent/tools/reflect.py` (`run_reviewer`, called from the `review` tool) — an independent one-shot `query()` whose critique the tool folds into its structured output — and the `extract` path of `fetch_example` in `agent/tools/example.py` (`extract_answer`), the same shape applied to data augmentation.

**Routing whole tool families:** there is a standing tension between the bitter-lesson instinct — give the agent every tool and let the model decide — and context economy: every schema wired into the main agent occupies its context, and a large enough surface starts deferring schemas on Claude harnesses (see [Deferred Tool Schemas](#deferred-tool-schemas-tool-search)). The settled middle ground is **one delegating tool per family**: instead of serving every data tool to the main agent, expose a single `research` tool whose handler runs a nested agent holding the whole data-gathering family (search, fetch, markets, news). The main context carries one schema and receives structured findings; the specialist schemas — and the reasoning that used them — stay in the nested agent's context. Accept a batch of questions in one call so the handler can fan them out in parallel, and persist findings when later tasks will reuse them. The aib downstream repo (`refs/aib` when linked) carries a full-scale reference: its `research` tool moves ~35 data tools off the main agent, batches questions, resumes prior research sessions for follow-ups, and persists findings to a worldview store.

**When to use each:** The axis is **context separation**. **Native subagents** extend the main agent's thinking — same session, shared context, like a specialized lobe that makes reasoning more efficient. **Nested agents** are for truly separable work — the two contexts shouldn't pollute each other. The main agent doesn't need the nested agent's reasoning chain, just its conclusion. The tool handler acts as a context boundary.

---

## Background Agent Pattern

For persistent agents that need parallel processing, a **background agent**
runs alongside the main agent using an injected configured factory. Immutable
Pydantic state is supplied on each wake and rapid wakes are debounced.

| Aspect        | Native Subagent              | Nested Agent                | Background Agent                |
| ------------- | ---------------------------- | --------------------------- | ------------------------------- |
| Lifetime      | Per-task (SDK dispatch)      | Per-tool-call               | Session-long                    |
| Runtime       | Main agent's session         | Independent via `query()`   | Independent configured factory |
| Initiation    | Agent dispatches via `Task()`| Tool handler creates on-demand| Wake events trigger turns       |
| Communication | SDK `ResultMessage`          | Tool return value           | Shared mutable state            |
| Use case      | Specialized long-running work| Quick generation, review    | Observation, research, execution|

**The shared-state pattern:** Background agents don't inject messages into the main agent's stream. Their tools write to shared objects (lists, dicts) that the main agent's tools read. The main agent pulls data when ready — no interruptions.

**Common use cases:**
- **Observer**: Summarizes conversation history so the main agent has context when earlier messages scroll out of the context window
- **Researcher**: Fetches and processes external data (with `builtin_tools=["Read", "Grep", "WebFetch"]`) while the main agent continues interacting
- **Executor**: Runs long-running tool calls without blocking the main agent's turns

**Lifecycle:** `start()` spawns an asyncio task. `wake(state)` copies the latest
typed state. A `state_to_request` callback builds the turn after debounce;
typed result/error handlers receive the outcome. `stop()` cancels the task and
session context cleanup aborts an unfinished native turn.

**Library support:** `lup.runtime.background.BackgroundAgent` composes only a
`SessionFactory`, `state_to_request`, typed result/error callbacks, and
`BackgroundConfig`. See the observer example in
`src/lup_template/agent/tools/realtime.py`.

**Customizing:** The `state_to_request` callback is the main extension point.
It translates immutable application state into a typed request without knowing
which provider owns the injected factory.

---

## Deferred Tool Schemas (Tool Search)

Claude harnesses (the CLI and the Agent SDK alike) stop loading every tool schema upfront once the combined schemas exceed a threshold — by default 10% of the model's context window (roughly 20k tokens at 200k). Beyond it, tools are **deferred**: the agent sees only names and must load a tool through the `ToolSearch` tool before calling it. This applies to built-in, MCP, and custom SDK tools.

**The failure mode:** an agent assumes a tool it "should" have does not exist — the schema is not in context, and a search with the wrong terms comes back empty (each search returns roughly the top five matches) — so it concludes the capability is missing and gives up without ever calling the tool.

**Configuration:** the `ENABLE_TOOL_SEARCH` environment variable controls deferral per session (`ClaudeAgentOptions(env=...)` in the SDK, shell environment for the CLI). Unset leaves the harness default (deferral on); `true` forces tool search on; `auto` defers past the default threshold; `auto:N` defers past N% of the context window; `false` loads every schema upfront with deferral disabled. `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` disables tool search entirely and cannot be overridden by `ENABLE_TOOL_SEARCH`. This template plumbs `AGENT_TOOL_SEARCH` (default `false`) into every Claude session it opens — the served tool surface is small and curated, so no schema should ever be invisible — and the repo's own dev harness pins `ENABLE_TOOL_SEARCH=false` in `.claude/settings.json` for the same reason.

**Prompt mitigations** when a large surface makes deferral worth keeping:

- Name the available tool *categories* in the system prompt ("you have tools for Slack, GitHub, and Jira — search for them") so the agent searches instead of concluding absence.
- Give tool families semantic name prefixes (`github_*`, `slack_*`) and write descriptions with the words a caller would actually use — search matches names and descriptions.
- Instruct the agent to search again with different terms before concluding a capability is missing.

**Native subagents** inherit the parent session's tool-search configuration — there is no per-subagent deferral override — and re-discover deferred tools themselves (search results are not shared with the parent). `AgentDefinition.tools` restricts which tools a subagent may use; it does not preload them.

**The structural fix** is to keep every agent's tool surface small enough that nothing defers: route whole tool families behind one delegating tool whose nested agent holds the family (see [Nested Agent Pattern](#nested-agent-pattern)) — each family's schemas then load next to the work that uses them.

Sources: [tool search (API)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool.md), [tool search (Agent SDK)](https://code.claude.com/docs/en/agent-sdk/tool-search.md), [managing tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context.md), [SDK MCP](https://code.claude.com/docs/en/agent-sdk/mcp.md), [SDK subagents](https://code.claude.com/docs/en/agent-sdk/subagents.md).

---

## Data Augmentation Pattern

Tools that fetch external data should **enrich it inside the tool** before returning to the agent. The agent receives structured, domain-aware results — not raw HTML, API responses, or search snippets it has to interpret.

| Do This                                                | Not This                                              |
| ------------------------------------------------------ | ----------------------------------------------------- |
| Tool recognizes URL domain, calls structured API       | Tool returns raw HTML for agent to parse               |
| Search results include API data for known domains      | Agent fetches each search result separately            |
| Null fields filled from fallback sources inside client | Agent retries with different queries to fill gaps      |
| Domain routing dispatches to specialized handlers      | Agent decides which tool to call per URL               |
| Enrichment runs in parallel inside the tool            | Agent sequentially processes each result               |

**The principle:** Every layer of the fetch pipeline automatically upgrades raw external data to structured, domain-appropriate content before it reaches the agent. The agent never parses HTML, never matches URL patterns, never decides which API to call for a given domain.

**Three forms of augmentation:**

1. **Domain dispatch** — URL patterns route to specialized API handlers (e.g., a wiki URL → structured article text via the wiki's API, instead of scraping HTML). Hints redirect the agent to a better tool when no direct handler exists.
2. **Null-filling** — Multi-source fallback pipelines that recover missing fields from alternative endpoints or sibling records (e.g., primary API withholds fields → fallback endpoint fills the gaps).
3. **Extraction** — Nested agent calls that distill large text blocks into focused answers (see [Nested Agent Pattern](#nested-agent-pattern)).

**Example:** `src/lup_template/agent/tools/example.py` is the template for all three forms — `fetch_example` routes known hosts to a specialized handler (`fetch_wiki_article`, domain dispatch) and distills fetched pages through a nested `query()` call (`extract_answer`, extraction); `search_example` recovers missing snippet fields from a fallback source (`fill_missing_snippets`, null-filling).

**Customizing:** Domain dispatch routes belong in `agent/tools/`. Build them lazily to avoid circular imports. Null-filling logic lives in API wrappers. Extraction uses `query(factory, request)` (see [Nested Agent Pattern](#nested-agent-pattern)).
