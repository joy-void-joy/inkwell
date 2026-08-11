<!-- Generated from lup.devtools.harness.content.docs.architecture by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Capability-composition architecture

Lup uses one independently constructible capability per ABC. A capability has
one to three cohesive abstract behavior methods, no concrete behavior or
properties, inherits only `ABC` plus typing generics, and is never combined
with another capability through multiple inheritance. Small callbacks remain
typed callables. `lup.codescan.capabilities` enforces the mechanical shape
across resolved project imports with the audited `abc-capability` rule.

Rich behavior is explicit data flow. `SessionHandle` contains a `Session` and
an optional `ForkSession`; `TurnHandle[T]` contains a `Turn[T]` and optional
live events, interrupt, and steer capabilities. These frozen Pydantic values
do not implement behavior or hide a provider. Unsupported behavior is absent.

Reaching a capability through a handle is one of the two stated exceptions to
the engine-versus-surface split (`docs/patterns.md`), and the criterion is
behavior: a frozen value that only carries capabilities is a transparent
carrier, not a caller-facing surface, so there is nothing for a composing
class to home. `handle.session.start(...)` and `turn.turn.result()` are
conforming. `SessionFactory` is the behavioral surface over these seams.

The runtime sequence is:

1. an application builds a validated Claude or Codex config;
2. immutable profile/endpoint transforms run before factory construction;
3. `SessionFactory.open()` owns provider resources;
4. `Session.start()` creates a fresh output store, finishes tool binding, and
   waits for native turn acknowledgement;
5. `Turn.result()` returns one strict `TurnResult[T]` or raises a typed error
   carrying all available blocks, usage, duration, identifiers, and validation
   history.

Timeout, budget, recovery, correction, serialization, observation, and
persistence are concrete decorators around these boundaries. Completed replay
is derived from `TurnResult.blocks`; only a native feed implements
`EventStream`.

## The adapter seam

Shared runtime, harness, policy, and resolver packages never import concrete
adapters or assemble provider wire names. Native config, hook payloads, command
spellings, manifests, and schemas remain inside adapter packages and concrete
CLI composition roots. A third adapter can implement the contracts without
editing a shared registry.

The same boundary applies to generation. Shared inspection and materialization
consume an injected immutable recipe; adapter selection is confined to the CLI
composition root. The generic path never compares a target name or provider
value. [harness.md](harness.md) walks that pipeline, and
[platform-differentiation.md](platform-differentiation.md) records every
difference the seam admits.

## Structured output has one mechanism

Each typed turn binds `submit_output` to its Pydantic schema and a fresh
store; native structured-output modes remain off. Validation and an optional
reflection gate run before persistence. A missing submission cannot be
represented as a successful typed result.

## Factories are chosen, never inferred

Applications choose factories explicitly. Immutable `ModelRoute` values may
select configured recipes, but model names never trigger optional SDK imports
at module import time and unknown models fail closed.

## Where each component sits

The capability rules above are what let the three components stay separable:
[library.md](library.md) describes the contracts and their implementations,
[template.md](template.md) describes the application that composes them, and
[permissions.md](permissions.md) describes the one decision path that has to
hold identically inside the library and inside a generated plugin that cannot
import it.
