"""Centralized Agent SDK client creation and response collection.

All Agent SDK client construction goes through this module to ensure
consistent defaults (session persistence disabled for nested agent calls).

Exports:
- ResponseCollector — response accumulator with .text and .output(T) accessors
- build_client() — AsyncContextManager[ClaudeSDKClient] with defaults
- query(prompt, ...) — build + query + collect; returns ResponseCollector or T

Examples:
    One-shot query (text result)::

        >>> collector = await query("Summarize this text", model="sonnet")
        >>> collector.text
        'Here is the summary...'

    Structured output via ``output_type`` (returns the model directly)::

        >>> from pydantic import BaseModel
        >>> class Summary(BaseModel):
        ...     title: str
        ...     points: list[str]
        >>> result = await query("Summarize X", output_type=Summary)
        >>> result.title
        'Summary of X'

    Nested agent with tools::

        >>> collector = await query(
        ...     "Review this code",
        ...     tools=["Read", "Grep"],
        ...     model="sonnet",
        ...     permission_mode="bypassPermissions",
        ...     max_turns=5,
        ... )
        >>> collector.text
        'The code looks correct...'

    Per-message handling with ``async for``::

        >>> async with build_client(tools=["Read"], model="sonnet") as client:
        ...     await client.query("Analyze main.py")
        ...     collector = ResponseCollector(client)
        ...     async for message in collector:
        ...         print_message(message)  # display as they arrive
        ...     # after iteration, all state is available
        ...     print(len(collector.blocks), "content blocks")
        ...     print(len(collector.tool_results), "tool results")

    Accessing collector state after ``query``::

        >>> collector = await query(
        ...     "List files", tools=["Bash"], max_turns=3,
        ... )
        >>> collector.text                  # concatenated assistant text
        >>> collector.blocks                # all ContentBlock objects
        >>> collector.tool_results          # tool result blocks from UserMessages
        >>> collector.messages              # full AssistantMessage/UserMessage list
        >>> collector.result                # final ResultMessage (or None)
        >>> collector.result.usage          # token usage from the session
"""

import asyncio
import contextvars
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, TypedDict, overload

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ContentBlock,
    Message,
    TextBlock,
)
from claude_agent_sdk.types import (
    AgentDefinition,
    AssistantMessage,
    HookEvent,
    HookMatcher,
    McpServerConfig,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    SystemPromptPreset,
    ToolsPreset,
    UserMessage,
)
from pydantic import BaseModel, ValidationError

from lup.hooks import create_large_read_hook, merge_hooks
from lup.trace import TraceLogger, active_agents, print_message

type HeartbeatCallback = Callable[[float], Coroutine[None, None, None]]
type BlockCallback = Callable[[ContentBlock, str], Coroutine[None, None, None]]

active_block_callback: contextvars.ContextVar[BlockCallback | None] = contextvars.ContextVar(
    "active_block_callback", default=None
)


class TokenUsage(TypedDict, total=False):
    """Token usage from Claude API responses."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output format types
# ---------------------------------------------------------------------------

JsonSchema = dict[str, object]
"""Type alias for JSON Schema payloads (from ``BaseModel.model_json_schema()``)."""

OutputFormat = dict[str, str | JsonSchema]
"""SDK output format dict (e.g. ``{"type": "json_schema", "schema": ...}``)."""


# ---------------------------------------------------------------------------
# JSON extraction from text fallback
# ---------------------------------------------------------------------------


def extract_json_object(text: str) -> object | None:  # claude: ignore
    """Try to pull a JSON object from *text*.

    Checks fenced code blocks first (```json ... ```), then falls back to
    finding the outermost ``{ ... }`` pair.  Returns the parsed object on
    success, ``None`` on failure.
    """
    fence_start = text.find("```json")
    if fence_start != -1:
        content_start = text.index("\n", fence_start) + 1
        fence_end = text.find("```", content_start)
        if fence_end != -1:
            try:
                return json.loads(text[content_start:fence_end])
            except (json.JSONDecodeError, ValueError):
                pass

    brace = text.find("{")
    if brace == -1:
        return None
    rbrace = text.rfind("}")
    if rbrace <= brace:
        return None
    try:
        return json.loads(text[brace : rbrace + 1])
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Response collector
# ---------------------------------------------------------------------------


class ResponseCollector:
    """Collects, displays, and logs agent response messages.

    Supports two usage patterns:

    **async for** — iterate messages yourself, no automatic display::

        collector = ResponseCollector(client, trace_logger=trace_logger)
        async for message in collector:
            print_message(message)

    **collect()** — drain all messages with automatic display and tracing::

        collector = ResponseCollector(client, trace_logger=trace_logger)
        result = await collector.collect()

    After iteration, access accumulated state:
    ``collector.blocks``, ``collector.tool_results``,
    ``collector.messages``, ``collector.result``.
    """

    def __init__(
        self,
        client: ClaudeSDKClient,
        trace_logger: TraceLogger | None = None,
        prefix: str = "",
        heartbeat: HeartbeatCallback | None = None,
        heartbeat_interval: float = 30.0,
        block_callback: BlockCallback | None = None,
    ) -> None:
        self.client = client
        self.blocks: list[ContentBlock] = []
        self.tool_results: list[ContentBlock] = []
        self.messages: list[AssistantMessage | UserMessage] = []
        self.result: ResultMessage | None = None
        self.trace_logger = trace_logger
        self.prefix = prefix
        self.heartbeat = heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.block_callback = block_callback

    @property
    def text(self) -> str | None:
        """Concatenated text from all assistant text blocks.

        Returns ``None`` when no text blocks were produced.  Access
        after ``collect()`` (called automatically by ``query()``).
        """
        texts = [b.text for b in self.blocks if isinstance(b, TextBlock)]
        return "\n\n".join(texts) if texts else None

    def output[T: BaseModel](self, output_type: type[T]) -> T | None:
        """Extract structured output as a validated Pydantic model.

        Handles the SDK wrapping bug where the agent produces
        ``{"output": <actual_data>}`` instead of ``<actual_data>`` directly.

        Falls back to extracting JSON from the text response when the model
        emits the schema-conformant object as text instead of through the
        structured output channel.

        Returns ``None`` when no valid output can be extracted.
        """
        data = (
            self.result.structured_output
            if self.result is not None and self.result.structured_output
            else extract_json_object(self.text or "")
        )
        if data is None:
            return None
        try:
            return output_type.model_validate(data)
        except ValidationError:
            if isinstance(data, dict) and "output" in data and len(data) == 1:
                return output_type.model_validate(data["output"])
            raise

    async def __aiter__(self) -> AsyncIterator[Message]:
        """Yield messages, accumulating state but not displaying.

        Raises RuntimeError on agent error results.
        """
        async for message in self.client.receive_response():
            match message:
                case StreamEvent():
                    continue

                case AssistantMessage():
                    self.messages.append(message)
                    for block in message.content:
                        self.blocks.append(block)

                case ResultMessage():
                    self.result = message
                    if message.is_error:
                        raise RuntimeError(f"Agent error: {message.result}")

                case SystemMessage():
                    logger.info("System [%s]: %s", message.subtype, message.data)

                case UserMessage():
                    self.messages.append(message)
                    if isinstance(message.content, list):
                        for block in message.content:
                            self.tool_results.append(block)

            yield message

    async def collect(self) -> ResultMessage:
        """Drain all messages, displaying and tracing each one.

        When a heartbeat callback is set, it fires every
        ``heartbeat_interval`` seconds with the elapsed time while
        waiting for model output. Stops as soon as a message arrives.

        Raises:
            RuntimeError: If the agent returns an error or no result.
        """
        label = self.prefix.strip() if self.prefix else ""
        if label:
            active_agents.add(label)

        heartbeat_task: asyncio.Task[None] | None = None
        start = time.monotonic()

        async def run_heartbeat() -> None:
            assert self.heartbeat is not None
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await self.heartbeat(time.monotonic() - start)

        if self.heartbeat is not None:
            heartbeat_task = asyncio.create_task(run_heartbeat())

        try:
            async for message in self:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    heartbeat_task = None
                if isinstance(message, StreamEvent):
                    continue
                print_message(
                    message,
                    prefix=self.prefix,
                    trace=self.trace_logger,
                )
                if self.block_callback is not None:
                    blocks: list[ContentBlock] = []
                    match message:
                        case AssistantMessage() | UserMessage():
                            if isinstance(message.content, list):
                                blocks = message.content
                    for block in blocks:
                        await self.block_callback(block, self.prefix)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            active_agents.discard(label)

        if self.result is None:
            raise RuntimeError("No result received from agent")
        return self.result


# ---------------------------------------------------------------------------
# Cost accumulator
# ---------------------------------------------------------------------------


def input_tokens_from_usage(usage: Mapping[str, object]) -> int:
    """Prompt-side tokens including cache creation and cache reads.

    The raw ``input_tokens`` field excludes cached context, which makes
    long agent runs look like they read almost nothing.
    """
    total = 0
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


class StageCostState(TypedDict):
    cost_usd: float
    duration_ms: float
    input_tokens: int
    output_tokens: int
    call_count: int


class CostState(TypedDict):
    total_cost_usd: float
    total_duration_ms: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    stages: dict[str, StageCostState]


class StageCost:
    """Cost breakdown for a single pipeline stage."""

    def __init__(self) -> None:
        self.cost_usd: float = 0.0
        self.duration_ms: float = 0.0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.call_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000


class CostAccumulator:
    """Tracks cumulative cost, duration, and token usage across multiple query() calls.

    Thread-safe for sequential use. Create one per pipeline run and pass it
    to each query() call via the ``cost_accumulator`` parameter.
    """

    def __init__(self) -> None:
        self.total_cost_usd: float = 0.0
        self.total_duration_ms: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.call_count: int = 0
        self.stages: dict[str, StageCost] = {}

    def record(self, result: ResultMessage, *, stage: str | None = None) -> None:
        """Record cost and usage from a completed query."""
        self.call_count += 1
        if result.total_cost_usd is not None:
            self.total_cost_usd += result.total_cost_usd
        if result.duration_ms is not None:
            self.total_duration_ms += result.duration_ms
        if result.usage:
            self.total_input_tokens += input_tokens_from_usage(result.usage)
            self.total_output_tokens += result.usage.get("output_tokens", 0)

        if stage is not None:
            if stage not in self.stages:
                self.stages[stage] = StageCost()
            sc = self.stages[stage]
            sc.call_count += 1
            if result.total_cost_usd is not None:
                sc.cost_usd += result.total_cost_usd
            if result.duration_ms is not None:
                sc.duration_ms += result.duration_ms
            if result.usage:
                sc.input_tokens += input_tokens_from_usage(result.usage)
                sc.output_tokens += result.usage.get("output_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def duration_seconds(self) -> float:
        return self.total_duration_ms / 1000

    def state_dict(self) -> CostState:
        """Serializable snapshot for carrying costs across process restarts."""
        return CostState(
            total_cost_usd=self.total_cost_usd,
            total_duration_ms=self.total_duration_ms,
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            call_count=self.call_count,
            stages={
                name: StageCostState(
                    cost_usd=sc.cost_usd,
                    duration_ms=sc.duration_ms,
                    input_tokens=sc.input_tokens,
                    output_tokens=sc.output_tokens,
                    call_count=sc.call_count,
                )
                for name, sc in self.stages.items()
            },
        )

    def load_state(self, state: CostState) -> None:
        """Seed the accumulator from a persisted snapshot (additive)."""
        self.total_cost_usd += state["total_cost_usd"]
        self.total_duration_ms += state["total_duration_ms"]
        self.total_input_tokens += state["total_input_tokens"]
        self.total_output_tokens += state["total_output_tokens"]
        self.call_count += state["call_count"]
        for name, sc_state in state["stages"].items():
            sc = self.stages.setdefault(name, StageCost())
            sc.cost_usd += sc_state["cost_usd"]
            sc.duration_ms += sc_state["duration_ms"]
            sc.input_tokens += sc_state["input_tokens"]
            sc.output_tokens += sc_state["output_tokens"]
            sc.call_count += sc_state["call_count"]


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


@asynccontextmanager
async def build_client(
    *,
    options: ClaudeAgentOptions | None = None,
    model: str | None = None,
    system_prompt: str | SystemPromptPreset | None = None,
    tools: list[str] | ToolsPreset | None = None,
    allowed_tools: list[str] | None = None,
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"]
    | None = None,
    mcp_servers: dict[str, McpServerConfig] | str | Path | None = None,
    agents: dict[str, AgentDefinition] | None = None,
    max_thinking_tokens: int | None = None,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    output_format: OutputFormat | None = None,
    extra_args: dict[str, str | None] | None = None,
    hooks: dict[HookEvent, list[HookMatcher]] | None = None,
) -> AsyncIterator[ClaudeSDKClient]:
    """Return a configured ClaudeSDKClient with project-wide defaults.

    Pass ``options`` (pre-built) to use as-is, or keyword arguments to
    construct ClaudeAgentOptions.  When using keyword arguments, always
    injects ``no-session-persistence`` into extra_args (caller wins on
    conflict).
    """
    if options is None:
        merged_extra: dict[str, str | None] = {
            "no-session-persistence": None,
            **(extra_args or {}),
        }
        default_hooks = create_large_read_hook()
        merged_hooks = merge_hooks(default_hooks, hooks) if hooks else default_hooks
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            allowed_tools=allowed_tools if allowed_tools is not None else [],
            permission_mode=permission_mode,
            mcp_servers=mcp_servers if mcp_servers is not None else {},
            agents=agents,
            max_thinking_tokens=max_thinking_tokens,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            output_format=output_format,
            extra_args=merged_extra,
            hooks=merged_hooks,
            include_partial_messages=True,
        )

    options.env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "128000")

    async with ClaudeSDKClient(options=options) as client:
        yield client


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


@overload
async def query(
    prompt: str,
    *,
    options: ClaudeAgentOptions | None = ...,
    prefix: str = ...,
    trace_logger: TraceLogger | None = ...,
    cost_accumulator: CostAccumulator | None = ...,
    model: str | None = ...,
    system_prompt: str | SystemPromptPreset | None = ...,
    tools: list[str] | ToolsPreset | None = ...,
    allowed_tools: list[str] | None = ...,
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"]
    | None = ...,
    mcp_servers: dict[str, McpServerConfig] | str | Path | None = ...,
    agents: dict[str, AgentDefinition] | None = ...,
    max_thinking_tokens: int | None = ...,
    max_turns: int | None = ...,
    max_budget_usd: float | None = ...,
    output_format: OutputFormat | None = ...,
    extra_args: dict[str, str | None] | None = ...,
    hooks: dict[HookEvent, list[HookMatcher]] | None = ...,
    heartbeat: HeartbeatCallback | None = ...,
    heartbeat_interval: float = ...,
    block_callback: BlockCallback | None = ...,
) -> ResponseCollector: ...


@overload
async def query[T: BaseModel](
    prompt: str,
    *,
    output_type: type[T],
    options: ClaudeAgentOptions | None = ...,
    prefix: str = ...,
    trace_logger: TraceLogger | None = ...,
    cost_accumulator: CostAccumulator | None = ...,
    model: str | None = ...,
    system_prompt: str | SystemPromptPreset | None = ...,
    tools: list[str] | ToolsPreset | None = ...,
    allowed_tools: list[str] | None = ...,
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"]
    | None = ...,
    mcp_servers: dict[str, McpServerConfig] | str | Path | None = ...,
    agents: dict[str, AgentDefinition] | None = ...,
    max_thinking_tokens: int | None = ...,
    max_turns: int | None = ...,
    max_budget_usd: float | None = ...,
    output_format: OutputFormat | None = ...,
    extra_args: dict[str, str | None] | None = ...,
    hooks: dict[HookEvent, list[HookMatcher]] | None = ...,
    heartbeat: HeartbeatCallback | None = ...,
    heartbeat_interval: float = ...,
    block_callback: BlockCallback | None = ...,
) -> T | None: ...


async def query(
    prompt: str,
    *,
    output_type: type[BaseModel] | None = None,
    options: ClaudeAgentOptions | None = None,
    prefix: str = "",
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    model: str | None = None,
    system_prompt: str | SystemPromptPreset | None = None,
    tools: list[str] | ToolsPreset | None = None,
    allowed_tools: list[str] | None = None,
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"]
    | None = None,
    mcp_servers: dict[str, McpServerConfig] | str | Path | None = None,
    agents: dict[str, AgentDefinition] | None = None,
    max_thinking_tokens: int | None = None,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    output_format: OutputFormat | None = None,
    extra_args: dict[str, str | None] | None = None,
    hooks: dict[HookEvent, list[HookMatcher]] | None = None,
    heartbeat: HeartbeatCallback | None = None,
    heartbeat_interval: float = 30.0,
    block_callback: BlockCallback | None = None,
) -> ResponseCollector | BaseModel | None:
    """Query an SDK client and collect the full response.

    Without ``output_type``: returns a ``ResponseCollector`` with
    ``.text``, ``.output(T)``, ``.blocks``, ``.messages``, ``.result``.

    With ``output_type``: returns a validated Pydantic model (or ``None``
    if the agent produced no structured output).

    Pass ``options`` (pre-built) to use as-is, or keyword arguments to
    construct ``ClaudeAgentOptions``.
    """
    if output_type is not None and output_format is None:
        schema = output_type.model_json_schema()
        output_format = {
            "type": "json_schema",
            "schema": schema,
        }
        schema_hint = (
            f"\n\nYour StructuredOutput must conform to this JSON schema "
            f"(output the data directly at the root level, do NOT wrap it "
            f"in an `output` key):\n```json\n"
            f"{json.dumps(schema, indent=2)}\n```"
        )
        if isinstance(system_prompt, str):
            system_prompt = system_prompt + schema_hint
        elif system_prompt is None:
            system_prompt = schema_hint.lstrip()

    resolved_callback = block_callback or active_block_callback.get()

    async with build_client(
        options=options,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        mcp_servers=mcp_servers,
        agents=agents,
        max_thinking_tokens=max_thinking_tokens,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format=output_format,
        extra_args=extra_args,
        hooks=hooks,
    ) as client:
        await client.query(prompt)
        collector = ResponseCollector(
            client,
            prefix=prefix,
            trace_logger=trace_logger,
            heartbeat=heartbeat,
            heartbeat_interval=heartbeat_interval,
            block_callback=resolved_callback,
        )
        await collector.collect()

    if cost_accumulator is not None and collector.result is not None:
        stage_name: str | None = None
        if prefix:
            stripped = prefix.strip()
            if stripped.startswith("[") and "]" in stripped:
                stage_name = stripped[1 : stripped.index("]")]
        cost_accumulator.record(collector.result, stage=stage_name)

    if output_type is not None:
        return collector.output(output_type)
    return collector
