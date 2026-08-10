"""Inkwell's session surface over lup's provider-neutral runtime.

Every pipeline stage, nested agent, and format adapter reaches a model through
:func:`query` here. The one place a concrete adapter is named is
:func:`provider_factory`; everything above it holds a ``SessionFactory`` and
knows nothing about which runtime answers.

The keyword surface is inkwell's own rather than lup's: a stage says what it
wants — a model, a prompt, tools, a trace logger, a cost accumulator — and this
module turns that into one configured session, runs one turn on it, and hands
back what the caller asked for. lup's ``TurnResult`` is the whole answer; a
call that named an ``output_type`` gets the validated model out of it, because
that is all a stage ever wants from a structured turn.
"""

import asyncio
import contextvars
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import overload

from pydantic import BaseModel, ConfigDict, Field

from lup.adapters.claude.selection import CLAUDE_RUNTIME
from lup.runtime.selection import SessionAutonomy, SessionRequest
from lup.hooks import LupHooksConfig, create_large_read_hook
from lup.mcp import McpServerEntry
from lup.runtime.errors import TurnInterruptedError
from lup.runtime.factory import SessionFactory
from lup.runtime.models import AnyTurnBlock, SessionId, TurnResult, turn_request
from lup.runtime.usage import CostAccumulator
from lup.runtime.wrappers import (
    DisplayConfig,
    DisplayRecord,
    TraceRecord,
    TracingConfig,
    UsageConfig,
    decorated_session_factory,
)
from lup.telemetry.trace import TraceEvent, TraceLogger
from lup.types import EnvVars, JsonValue

logger = logging.getLogger(__name__)

type BlockCallback = Callable[[AnyTurnBlock, str], Coroutine[None, None, None]]
"""Async hook fired once per completed block, with the caller's trace prefix."""

type HeartbeatCallback = Callable[[float], Coroutine[None, None, None]]
"""Async hook fired every interval a turn is still running, with its elapsed seconds."""

type SessionCapture = Callable[[str, str], Coroutine[None, None, None]]
"""Async hook that records ``(stage_label, session_id)`` durably."""

type TraceSink = Callable[[TraceRecord], Coroutine[None, None, None]]
type DisplaySink = Callable[[DisplayRecord], Coroutine[None, None, None]]


class ActiveAgent(BaseModel):
    """One turn in flight, as a surface watching the run sees it."""

    model_config = ConfigDict(frozen=True)

    label: str
    model: str | None = None
    started: datetime = Field(default_factory=datetime.now)

    def elapsed(self) -> timedelta:
        """How long this turn has been running."""
        return datetime.now() - self.started


active_agents: list[ActiveAgent] = []
"""Every turn currently in flight, in the order they started.

A record rather than a bare label, because a surface watching a long run
wants more than which stages are working: a writer six minutes in and one
that just started read very differently on the same toolbar, and the model a
stage was routed to is the first thing asked when its output surprises. A
list rather than a set — two turns can share a label, and the second one
finishing must not retire the first.
"""

active_block_callback: contextvars.ContextVar[BlockCallback | None] = (
    contextvars.ContextVar("active_block_callback", default=None)
)
"""Where completed blocks are forwarded for the current task tree.

Set once at the root of a run by whatever is displaying it, so every nested
`query()` beneath reports to the same listener without a callback threaded
through every stage signature.
"""


class SessionPolicy(BaseModel):
    """Run-scoped policy that makes nested ``query()`` calls resumable.

    ``cwd`` pins the transcript namespace (it must match to resume). For each
    call, its stage label (the ``[tag]`` prefix) maps through ``resume_for`` to
    a prior session id to continue — or ``None`` for a fresh one — and through
    ``capture`` the id is recorded the instant it is known, so an interrupted
    run can be resumed later.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    cwd: str | None
    resume_for: Callable[[str], str | None]
    capture: SessionCapture


session_policy: contextvars.ContextVar[SessionPolicy | None] = contextvars.ContextVar(
    "session_policy", default=None
)
"""Active session policy for the current task tree (see ``SessionPolicy``).

Unset by default, so ``query()`` stays one-shot and non-persistent. A caller
that wants resumable nested agents sets it once at the root of a run."""


client_env: contextvars.ContextVar[EnvVars | None] = contextvars.ContextVar(
    "client_env", default=None
)
"""Environment injected into every session this task tree opens.

The provider runs its CLI as a subprocess that authenticates from its own
environment (``CLAUDE_CONFIG_DIR``, ``ANTHROPIC_*``), so the account billed for
inference is decided there rather than by anything passed to ``query()``. Set
this at the root of a session to route the subprocess to session-specific
credentials. Each asyncio task tree gets its own copy, so concurrent profiles
do not race.
"""


def is_interrupt(exc: BaseException) -> bool:
    """True when a turn stopped because something interrupted it, not failed.

    Two shapes reach here. An interrupt the session *asked for* is classified
    at the adapter and arrives typed. A SIGINT from outside — a reloading dev
    server, a Ctrl-C in the terminal — kills the provider's subprocess with no
    request behind it, so it arrives as an ordinary provider error whose only
    mark is the exit code. Both are a stop the run can be resumed from, which
    is the question every caller here is asking.
    """
    return isinstance(exc, TurnInterruptedError) or "exit code -2" in str(exc)


def stage_label(prefix: str) -> str | None:
    """The ``[tag]`` at the head of a trace prefix, used as a stage label."""
    stripped = prefix.strip()
    if stripped.startswith("[") and "]" in stripped:
        return stripped[1 : stripped.index("]")]
    return None


def fenced_json(text: str) -> JsonValue:
    """The object inside the first ```json fence, if there is a parsable one."""
    fence = text.find("```json")
    if fence == -1:
        return None
    start = text.index("\n", fence) + 1
    end = text.find("```", start)
    if end == -1:
        return None
    try:
        return json.loads(text[start:end])
    except ValueError:
        return None


def extract_json_object(text: str) -> JsonValue:
    """Try to pull a JSON object out of *text*.

    A fenced block first, then the outermost brace pair. The fence wins
    because a model that wrote one meant *that* to be the answer — whatever
    came before it is reasoning, and reasoning about JSON often contains
    JSON. Returns nothing when neither reading parses.
    """
    fenced = fenced_json(text)
    if fenced is not None:
        return fenced

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


@asynccontextmanager
async def beating(
    heartbeat: HeartbeatCallback | None, interval: float
) -> AsyncGenerator[None]:
    """Report elapsed seconds every interval for as long as the body runs.

    A writing stage can think for many minutes without emitting a block, and
    a surface with nothing to show reads as a hung one. The ticking is the
    caller's own — this only says how long it has been.
    """
    if heartbeat is None:
        yield
        return

    started = time.monotonic()

    async def tick() -> None:
        while True:
            await asyncio.sleep(interval)
            await heartbeat(time.monotonic() - started)

    beat = asyncio.create_task(tick())
    try:
        yield
    finally:
        beat.cancel()


def result_text[T: BaseModel | None](result: TurnResult[T]) -> str:
    """Concatenate the turn's completed text blocks."""
    return "\n\n".join(
        text for block in result.blocks if (text := block.text_payload) is not None
    )


RUNTIME = CLAUDE_RUNTIME
"""The one place inkwell names a provider.

Everything downstream reads this: :func:`provider_factory` opens sessions
through it, and the profile system administers the login it carries. Pointing
this at another runtime moves both, so the two cannot come to disagree about
which provider inkwell is running.
"""

PROVIDER_LOGIN = RUNTIME.login
"""Where the selected runtime keeps a login, and how to point it at one."""


def provider_factory(
    *,
    model: str | None = None,
    system_prompt: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    autonomy: SessionAutonomy | None = None,
    tool_servers: dict[str, McpServerEntry] | None = None,
    max_thinking_tokens: int | None = None,
    max_turns: int | None = None,
    cwd: Path | None = None,
    hooks: LupHooksConfig | None = None,
) -> SessionFactory:
    """Open a session through the selected runtime.

    Every caller above receives only a configured ``SessionFactory`` and
    cannot tell which runtime answers it — the selection itself is
    :data:`RUNTIME`, and this states what a session should be, not who runs
    it.
    """
    return RUNTIME.session_factory(
        SessionRequest(
            model=model,
            instructions=system_prompt,
            tools=tools,
            allowed_tools=allowed_tools or [],
            tool_servers=tool_servers or {},
            autonomy=autonomy,
            max_turns=max_turns,
            max_thinking_tokens=max_thinking_tokens,
            cwd=cwd,
            environment=client_env.get() or {},
            hooks=hooks if hooks is not None else create_large_read_hook(),
        )
    )


def trace_sink(trace_logger: TraceLogger) -> TraceSink:
    """Record a turn's terminal outcome, and save the trace either way."""

    async def record(entry: TraceRecord) -> None:
        if not entry.succeeded and entry.failure is not None:
            for block in entry.failure.blocks:
                trace_logger.log_block(block.telemetry_block)
            trace_logger.log_text(entry.failure.message, heading="Turn error")
            trace_logger.emit_event(
                TraceEvent(
                    kind="error",
                    timestamp=datetime.now().isoformat(),
                    brief=entry.failure.message,
                )
            )
        trace_logger.save()

    return record


def display_sink(
    trace_logger: TraceLogger | None, block_callback: BlockCallback | None, prefix: str
) -> DisplaySink:
    """Log every completed block, and forward it to whoever is watching."""

    async def show(entry: DisplayRecord) -> None:
        for block in entry.blocks:
            if trace_logger is not None:
                trace_logger.log_block(block.telemetry_block)
            if block_callback is not None:
                await block_callback(block, prefix)

    return show


def observed_factory(
    factory: SessionFactory,
    *,
    label: str | None,
    prefix: str,
    trace_logger: TraceLogger | None,
    cost_accumulator: CostAccumulator | None,
    block_callback: BlockCallback | None,
) -> SessionFactory:
    """Wire this call's trace, display, and billing onto a built factory.

    The stage label is bound here rather than read from ambient state, which
    is what lets two sections write concurrently and still bill apart.
    """
    return decorated_session_factory(
        factory,
        tracing=(
            TracingConfig(sink=trace_sink(trace_logger))
            if trace_logger is not None
            else None
        ),
        usage=(
            UsageConfig(sink=cost_accumulator.sink(label))
            if cost_accumulator is not None
            else None
        ),
        display=(
            DisplayConfig(sink=display_sink(trace_logger, block_callback, prefix))
            if trace_logger is not None or block_callback is not None
            else None
        ),
    )


@overload
async def query[T: BaseModel](
    prompt: str,
    *,
    output_type: type[T],
    model: str | None = ...,
    system_prompt: str = ...,
    tools: list[str] | None = ...,
    allowed_tools: list[str] | None = ...,
    autonomy: SessionAutonomy | None = ...,
    mcp_servers: dict[str, McpServerEntry] | None = ...,
    max_thinking_tokens: int | None = ...,
    max_turns: int | None = ...,
    prefix: str = ...,
    trace_logger: TraceLogger | None = ...,
    cost_accumulator: CostAccumulator | None = ...,
    block_callback: BlockCallback | None = ...,
    heartbeat: HeartbeatCallback | None = ...,
    heartbeat_interval: float = ...,
    hooks: LupHooksConfig | None = ...,
    cwd: Path | None = ...,
    resume: str | None = ...,
) -> T | None: ...


@overload
async def query(
    prompt: str,
    *,
    model: str | None = ...,
    system_prompt: str = ...,
    tools: list[str] | None = ...,
    allowed_tools: list[str] | None = ...,
    autonomy: SessionAutonomy | None = ...,
    mcp_servers: dict[str, McpServerEntry] | None = ...,
    max_thinking_tokens: int | None = ...,
    max_turns: int | None = ...,
    prefix: str = ...,
    trace_logger: TraceLogger | None = ...,
    cost_accumulator: CostAccumulator | None = ...,
    block_callback: BlockCallback | None = ...,
    heartbeat: HeartbeatCallback | None = ...,
    heartbeat_interval: float = ...,
    hooks: LupHooksConfig | None = ...,
    cwd: Path | None = ...,
    resume: str | None = ...,
) -> TurnResult[None]: ...


async def query(
    prompt: str,
    *,
    output_type: type[BaseModel] | None = None,
    model: str | None = None,
    system_prompt: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    autonomy: SessionAutonomy | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    max_thinking_tokens: int | None = None,
    max_turns: int | None = None,
    prefix: str = "",
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    block_callback: BlockCallback | None = None,
    heartbeat: HeartbeatCallback | None = None,
    heartbeat_interval: float = 30.0,
    hooks: LupHooksConfig | None = None,
    cwd: Path | None = None,
    resume: str | None = None,
) -> BaseModel | None | TurnResult[None]:
    """Open one configured session, run one turn on it, and close it.

    With ``output_type``, returns the validated model the turn submitted, or
    ``None`` when it submitted nothing. Without one, returns the whole
    ``TurnResult`` — its blocks are what a caller reading free prose wants.
    """
    label = stage_label(prefix)
    policy = session_policy.get()
    resumed = resume
    if policy is not None:
        cwd = cwd if cwd is not None else (Path(policy.cwd) if policy.cwd else None)
        if label is not None and resumed is None:
            resumed = policy.resume_for(label)

    factory = observed_factory(
        provider_factory(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            allowed_tools=allowed_tools,
            autonomy=autonomy,
            tool_servers=mcp_servers,
            max_thinking_tokens=max_thinking_tokens,
            max_turns=max_turns,
            cwd=cwd,
            hooks=hooks,
        ),
        label=label,
        prefix=prefix,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
        block_callback=block_callback or active_block_callback.get(),
    )

    running = None if label is None else ActiveAgent(label=label, model=model)
    if running is not None:
        active_agents.append(running)
    try:
        async with factory.open(
            SessionId(value=resumed) if resumed else None
        ) as opened:
            # Each arm starts its own turn because `TurnRequest` is invariant:
            # a union of them binds no single output type at `start`.
            if output_type is None:
                turn = await opened.session.start(turn_request(prompt))
            else:
                turn = await opened.session.start(turn_request(prompt, output_type))
            async with beating(heartbeat, heartbeat_interval):
                result = await turn.turn.result()
            if policy is not None and label is not None:
                await policy.capture(label, result.identifiers.session.value)
    finally:
        if running is not None:
            active_agents.remove(running)

    return result if output_type is None else result.output
