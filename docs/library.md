<!-- Generated from lup.devtools.harness.content.docs.library by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# The lup library

`packages/lup` is the reusable half of this repository: a standalone published
package that knows how to run an agent turn, compile a harness, decide a
permission, and resolve reviewed feedback — without knowing which vendor is
behind any of it. It is the larger of the two code components and the one a
downstream project depends on.

The rule that shapes every module: **shared code never names a provider.**
Native config, hook payloads, command spellings, manifests, and wire schemas
live inside `lup/adapters/`. Everything above the adapters speaks contracts.
A third backend implements those contracts without editing a shared registry,
because there is no registry to edit.

## The front door

`packages/lup/src/lup/__init__.py` re-exports a deliberately small runtime
surface — the one place in the library where a barrel is allowed, because it
declares a public API:

```python
from lup import (
    SessionFactory,   # open configured conversations
    SessionHandle,    # an opened session, plus optional fork capability
    TurnHandle,       # an accepted turn, plus optional events/interrupt/steer
    TurnInput,        # portable user input
    TurnRequest,      # what to run, and the type to come back
    TurnResult,       # a validated, typed result
    turn_request,     # request factory
    query,            # one-shot: factory in, typed result out
)
```

The shortest useful program is one call:

```python
factory = create_claude_session_factory(ClaudeSessionConfig(model="..."))
result = await query(factory, turn_request(TurnInput(text="summarize"), Summary))
summary = result.output
```

`query` is the one-shot convenience. For anything with more than one turn, open
a session and start turns on it — the same contracts, held longer.

## Layering

Four tiers, and imports only ever point downward.

1. **`lup.types`** — the portable content and tool vocabulary every other
   package speaks: `JsonValue`/`JsonObject`, `ToolName`/`ToolGrant`,
   `LupContentBlock`, `LupMessage`, `Usage`, `SubagentSpec`.
2. **`contracts` and `models`** — each subpackage carries both. `models` owns
   the vocabulary; `contracts` owns the narrow capability seams. Contracts
   import types only, so a fake implementation needs nothing else.
3. **Implementations** — composition, wrappers, validation, reconciliation,
   rule evaluation. These import their own package's contracts and models, and
   nothing from `adapters`.
4. **`lup.adapters.claude` / `lup.adapters.codex`** — the only packages that
   name a vendor. They implement the contracts above and are imported only by
   named composition roots.

`lup.codescan.boundaries` enforces tier 4 mechanically with the
`seam-boundary` rule: a concrete adapter import outside `lup/adapters/`,
the tests, the examples, or a named application composition root is a
build failure, not a review comment.

## The packages

### `runtime` — how one turn runs

The engine. `contracts.py` declares the lifecycle seams — open a session,
start a turn, await a result — as one-to-three-method capabilities.
`models.py` holds the shared turn vocabulary: opaque `SessionId`/`TurnId`,
the `TurnBlock` union (`TurnTextBlock`, `TurnThinkingBlock`,
`TurnToolCallBlock`, `TurnToolResultBlock`), and the generic
`TurnRequest[T]`/`TurnResult[T]`.

Everything optional is a decorator or an absent capability, never a flag:
`wrappers.py` layers timeouts, budgets, retries, correction, tracing, usage,
and display around a factory; `routing.py` selects a configured recipe from an
immutable `ModelRoute` list and fails closed on an unknown model;
`background.py` coalesces state wakes into turns on a persistent session;
`output.py` binds a fresh `submit_output` tool and store to each typed turn.

Unsupported behavior is *absent* from the handle rather than present and
raising. If `TurnHandle.steer` is `None`, that backend cannot steer.

### `harness` — declaration to disk

Compiles one provider-neutral declaration into native plugin trees, with a
proof of what it owns. `models.py` holds the declaration graph
(`Harness` → `Plugin` → `Skill`/`Agent`/`HookSet`) and the rendered
`Artifact`/`ArtifactTree`. Prompt bodies are ordered typed parts —
`TextPart` for prose, `SkillInvocation`/`NativePath`/`ArgumentsRef` and their
siblings for anything a runtime spells its own way.

The pipeline is `validation` → `ownership` → `reconciliation` →
`materialization`, plus `proposals` for the reviewed patch transport back to
canonical source and `process`/`environment` for launching a native CLI.
`generation.py` holds the small deterministic helpers the stages share,
including the do-not-edit banner every commentable generated artifact opens
with. [harness.md](harness.md) walks the whole pipeline.

### `policy` — one decision, two homes

The permission core, split so the same verdict can be reached inside this
library and inside a generated plugin that cannot import it.

`policy/kernel/` is hermetic: stdlib-only, statically audited imports,
primitive rows in and a decision out. It is copied *verbatim* into every
generated tree, which is why a traceback from a hook still points at real
canonical line numbers. Above it, `rules.py` validates application inputs as
Pydantic surfaces and erases them into kernel rows, `chain.py` composes
policies deny-before-ask, and `bundle.py` assembles the kernel source plus
rendered data rows for generation. [permissions.md](permissions.md) is the
full lattice.

### `resolver` — reviewed feedback to an integration branch

A persisted state machine over concerns. `models.py` holds schema-versioned
records; `dag.py` validates and orders the concern graph; `state.py` persists
it atomically under a file lock; `run.py` names the one live state a run
holds, with the lock and the observer that guard it; `orchestrator.py` owns
every git side effect (leases, worktrees, commits, dependency bases);
`mailbox.py` carries questions and answers as files so any door can write
while the run holds its lease. Each phase is a collaborator over those rather
than a method on one class: `questions.py` publishes and promotes,
`actors.py` holds one durable session per actor, `turns.py` puts the prompts
to them, `joins.py` brings branches together and settles what that breaks,
`verification.py` runs one tree through the verification set, and
`execution.py` drives one concern's revision loop. `core.py` composes them
and owns only the sequence. [resolver.md](resolver.md) covers the lifecycle.

### `codescan` — the executable conventions

The rule engine behind `lup-devtools dev check` and both generated edit hooks.
`common.py` provides comment-column tokenization, docstring detection, and
ignore-directive parsing; `markers.py` finds `# lup:` review notes;
`antipatterns.py`, `boundaries.py`, `capabilities.py`, and `portable.py` are
the rule families; `registry.py` indexes them all into
[rules.md](rules.md).

### `adapters` — the vendor edge

`adapters/claude/` and `adapters/codex/` each implement the same four seams:
`runtime.py` (open sessions behind the runtime contracts), `harness.py`
(render the declaration into that runtime's tree), `harness_runtime.py`
(probe the installed CLI for evidence), and `native.py` (decode hook payloads
into policy events, render decisions back). `adapters/harness.py` composes the
renderers into whole-tree compilers.

Each also carries what only it needs: Claude a personal profile store, Codex a
typed JSON-RPC transport to `codex app-server`. Neither is mirrored for
symmetry's sake. [platform-differentiation.md](platform-differentiation.md)
is the map of every difference.

### The rest

| Package | Solves |
| --- | --- |
| `devtools` | The development CLI an adopter inherits rather than forks: worktrees and branches, trace and Python introspection, the resolver supervisor, the sync registry, version bookkeeping. Ships no roster — each sub-app declares itself beside its Typer app and an application composes the ones it wants. Requires the `web` extra for the supervisor. |
| `web` | What a page served on this machine does to stay local-only: the loopback bind refusal and the `Host` check that DNS rebinding would otherwise walk past. |
| `workspace` | Where a run's data lives: version-aware paths, the `SessionContext` that crosses a process boundary, session history, and the note directories a session may touch. |
| `realtime` | The wake/act/sleep lifecycle for persistent agents. `scheduler.py` stands alone; `relay.py` layers a subprocess mailbox transport on top and is never imported by it. |
| `telemetry` | What a run records about itself: markdown trace plus machine-readable sidecar, console rendering, per-tool metrics with a file-backed flush for subprocess tools. |
| `sandbox` | A Docker-isolated Python REPL — mount topology, container lifecycle, and the exec-multiplexed socket protocol. Requires the `docker` extra. |
| `resilience` | `throttle` bounds concurrency and minimum call interval; `retry` re-runs a coroutine with exponential backoff. |
| `hooks` | SDK-agnostic hook models and factories: permission hooks, tool allowlists, gates, nudges, capture. |
| `mcp` | The `lup_tool` decorator and `create_mcp_server`, with typed input models and error propagation that actually reaches the caller. |
| `reflect` | Reflect-before-output gates: a flag-based `ReflectionGate` and a verdict-aware `ReviewGate`. |

## Building on it

The library is the dependency; your application is the composition root. That
inversion is the whole design, and it has three practical consequences.

**Name the provider exactly once.** Choose an adapter factory in one function,
pass the resulting `SessionFactory` everywhere else. `seam-boundary` will tell
you when a second site appears.

**Compose capabilities rather than configuring an object.** Timeouts, budgets,
retries, persistence, and tracing are `DecoratingSessionFactory` layers you
add individually, not fields on a client.

**Let typed output be the only output.** Bind a Pydantic type to the turn and
read `TurnResult.output`. A missing submission raises a typed error carrying
the blocks, usage, duration, and validation history — it cannot arrive as an
empty success.

`src/lup_template` is the worked example of all three; see
[template.md](template.md).
