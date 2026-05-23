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

    Streaming with ``async for`` for per-message handling::

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

import logging
from collections.abc import AsyncIterator
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
    SystemMessage,
    SystemPromptPreset,
    ToolsPreset,
    UserMessage,
)
from pydantic import BaseModel

from lup.trace import TraceLogger, print_message


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
    ) -> None:
        self.client = client
        self.blocks: list[ContentBlock] = []
        self.tool_results: list[ContentBlock] = []
        self.messages: list[AssistantMessage | UserMessage] = []
        self.result: ResultMessage | None = None
        self.trace_logger = trace_logger
        self.prefix = prefix

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

        Returns ``None`` when the agent produced no structured output.
        """
        if self.result is not None and self.result.structured_output:
            return output_type.model_validate(self.result.structured_output)
        return None

    async def __aiter__(self) -> AsyncIterator[Message]:
        """Yield messages, accumulating state but not displaying.

        Raises RuntimeError on agent error results.
        """
        async for message in self.client.receive_response():
            match message:
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

        Raises:
            RuntimeError: If the agent returns an error or no result.
        """
        async for message in self:
            print_message(message, prefix=self.prefix, trace=self.trace_logger)

        if self.result is None:
            raise RuntimeError("No result received from agent")
        return self.result


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
            hooks=hooks,
        )

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
) -> ResponseCollector: ...


@overload
async def query[T: BaseModel](
    prompt: str,
    *,
    output_type: type[T],
    options: ClaudeAgentOptions | None = ...,
    prefix: str = ...,
    trace_logger: TraceLogger | None = ...,
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
) -> T | None: ...


async def query(
    prompt: str,
    *,
    output_type: type[BaseModel] | None = None,
    options: ClaudeAgentOptions | None = None,
    prefix: str = "",
    trace_logger: TraceLogger | None = None,
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
        output_format = {
            "type": "json_schema",
            "schema": output_type.model_json_schema(),
        }

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
        collector = ResponseCollector(client, prefix=prefix, trace_logger=trace_logger)
        await collector.collect()

    if output_type is not None:
        return collector.output(output_type)
    return collector
