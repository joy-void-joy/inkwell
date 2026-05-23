# Design Patterns

Architectural patterns used in this project. For daily development guidance, see [CLAUDE.md](CLAUDE.md).

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

**Library support:** `packages/lup/src/lup/realtime.py` provides the `Scheduler` class and hook factories (`create_stop_guard`, `create_pending_event_guard`). See example tools in `src/lup_template/agent/tools/realtime.py`.

---

## Reflection Pattern

Agents produce better output when forced to self-assess before committing. Three components:

1. **Reflection tool** (`agent/tools/reflect.py`): Domain-customizable self-assessment — confidence, uncertainties, tool audit, process reflection. Optionally runs a reviewer sub-agent.
2. **Reflection gate** (`lup.reflect`): `ReflectionGate` flag tracker + `create_reflection_gate()` hook factory. Denies a target tool until reflection occurs.
3. **Wiring**: The gate blocks `StructuredOutput` (one-shot agents) or `sleep` (persistent agents) until reflection occurs.

**Customizing:** The gate in `lup.reflect` is domain-neutral and parametric. The reflection tool and `ReflectInput` in `agent/tools/reflect.py` are domain-specific — add fields for your domain. The reviewer prompt should target your domain's common failure modes.

**Skip reviewer:** Set `skip_reviewer=True` for speed-sensitive or trivial tasks. The reviewer adds latency but catches calibration errors and reasoning gaps.

---

## Nested Agent Pattern

Distinct from **subagents** (SDK-native `Task()` dispatch, defined upfront in `get_subagents()`, same session). A nested agent is a tool that internally creates an independent SDK client, runs it, and folds the result back into its tool response.

| Aspect     | Subagent                          | Nested Agent                        |
| ---------- | --------------------------------- | ----------------------------------- |
| Definition | Upfront in `get_subagents()`      | On-demand inside a tool handler     |
| Client     | Main agent's SDK session          | Independent client via `query()`    |
| Session    | Shared — same trace, same metrics | Isolated — no session persistence   |
| Return     | SDK `ResultMessage` (structured)  | Scalar result augmented by the tool |
| Use case   | Specialized long-running work     | Quick generation, review, parsing   |

**The augmentation pattern:** The tool handler post-processes the nested agent's output before returning it. The nested agent produces raw material; the tool shapes it into the MCP response:

```python
@lup_tool("Review code quality and return structured assessment")
async def review(params: ReviewInput) -> ReviewOutput:
    collector = await query(
        build_review_prompt(params),
        model="sonnet",
        system_prompt=REVIEWER_PROMPT,
        tools=["Read", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=5,
    )
    # Augment: fold nested agent's text into structured tool output
    return ReviewOutput(critique=collector.text or "", score=compute_score(collector))
```

**Library support:** `query()` in `lup.client` handles the full pipeline (build client → query → collect). Session persistence is automatically disabled. Use `collector.text` for text extraction, `collector.output(T)` for structured output, or pass `output_type=T` to `query()` to get `T | None` directly.

**When to use each:** The axis is **context separation**. **Subagents** extend the main agent's thinking — same session, shared context, like a specialized lobe that makes reasoning more efficient. **Nested agents** are for truly separable work — the two contexts shouldn't pollute each other. The main agent doesn't need the nested agent's reasoning chain, just its conclusion. The tool handler acts as a context boundary.

---

## Background Agent Pattern

For persistent agents that need parallel processing, a **background agent** runs alongside the main agent for the entire session. It has its own SDK client and tools, communicates through shared mutable state, and processes events independently. Multiple background agents can coexist.

| Aspect        | Subagent                     | Nested Agent                | Background Agent                |
| ------------- | ---------------------------- | --------------------------- | ------------------------------- |
| Lifetime      | Per-task (SDK dispatch)      | Per-tool-call               | Session-long                    |
| Client        | Main agent's SDK session     | Independent via `query()`   | Independent `ClaudeSDKClient`   |
| Initiation    | Agent dispatches via `Task()`| Tool handler creates on-demand| Wake events trigger turns       |
| Communication | SDK `ResultMessage`          | Tool return value           | Shared mutable state            |
| Use case      | Specialized long-running work| Quick generation, review    | Observation, research, execution|

**The shared-state pattern:** Background agents don't inject messages into the main agent's stream. Their tools write to shared objects (lists, dicts) that the main agent's tools read. The main agent pulls data when ready — no interruptions.

**Common use cases:**
- **Observer**: Summarizes conversation history so the main agent has context when earlier messages scroll out of the context window
- **Researcher**: Fetches and processes external data (with `builtin_tools=["Read", "Grep", "WebFetch"]`) while the main agent continues interacting
- **Executor**: Runs long-running tool calls without blocking the main agent's turns

**Lifecycle:** `start()` spawns an asyncio task. `wake()` signals new data. The message generator debounces rapid wakes and calls `build_message()` to produce the next turn. `stop()` cancels the task.

**Library support:** `packages/lup/src/lup/background.py` provides the `BackgroundAgent` class. See observer example in `src/lup_template/agent/tools/realtime.py`.

**Customizing:** The `build_message` callback is the main extension point — it reads shared state, advances its own read pointer, and returns the next user turn content (or `None` to skip). The observer example in `agent/tools/realtime.py` shows the full wiring.

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

**Customizing:** Domain dispatch routes belong in `agent/tools/`. Build them lazily to avoid circular imports. Null-filling logic lives in API client wrappers. Extraction uses `query()` from `lup.client` (see [Nested Agent Pattern](#nested-agent-pattern)).
