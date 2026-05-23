"""Background agents for persistent sessions.

A background agent runs alongside a main agent for the entire session
lifetime. It has its own SDK client, tools, and system prompt, and
communicates with the main agent through shared mutable state.

Use cases:
- Observation: summarize conversations as they unfold
- Research: fetch and process data while the main agent continues
- Execution: run long-running tool calls without blocking
- Multiple agents can coexist in a single session

The pattern:
1. Create a BackgroundAgent with tools and a ``build_message`` callback
2. Start it — it runs as an asyncio task until stopped
3. Wake it when new data is available
4. It processes data via tool calls that write to shared state
5. The main agent reads results through its own tools

See ``src/lup_template/agent/tools/realtime.py`` for example integration with
the persistent agent pattern (observer example).

Examples:
    Create an observer that maintains conversation notes::

        >>> from lup.background import BackgroundAgent
        >>> notes: list[str] = []
        >>> agent = BackgroundAgent(
        ...     name="observer",
        ...     system_prompt="Summarize conversations...",
        ...     tools=create_observer_tools(notes=notes),
        ...     build_message=build_observer_message,
        ...     allowed_tools=["mcp__observer__notes"],
        ... )
        >>> agent.start()
        >>> agent.wake()  # signal new data
        >>> await agent.stop()

    Run multiple background agents in parallel::

        >>> observer = BackgroundAgent(name="observer", ...)
        >>> researcher = BackgroundAgent(
        ...     name="researcher",
        ...     builtin_tools=["Read", "Grep", "WebFetch"],
        ...     ...
        ... )
        >>> observer.start()
        >>> researcher.start()
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
)

logger = logging.getLogger(__name__)


class BackgroundAgent:
    """A companion agent running in parallel with a main session.

    Runs an independent SDK client with its own MCP tools and system
    prompt. Communicates with the main agent through shared mutable
    state — the background agent's tools write to objects (lists, dicts)
    that the main agent's tools read.

    Multiple BackgroundAgents can coexist. Each has its own wake event
    and processes independently.

    Args:
        name: Identifier (used as MCP server name and in logs).
        system_prompt: System prompt for the background agent.
        tools: MCP tool functions (``@tool``-decorated). Auto-creates
            an MCP server named ``name``.
        build_message: Called on each wake to produce the next user turn.
            Returns ``None`` to skip (no new data). Should read from
            shared state and advance its own read pointer.
        start_message: Initial user turn when the agent starts.
        model: Model to use. Defaults to Sonnet for cost efficiency.
        max_thinking_tokens: Thinking budget. Defaults to max.
        debounce_seconds: Batch rapid wakes — wait this long after
            a wake for more events before sending to the agent.
        builtin_tools: SDK built-in tools (``Read``, ``Grep``,
            ``WebFetch``, etc.) for agents that need them.
        allowed_tools: Restrict to these tool names. If ``None``,
            the SDK default applies.
        on_response: Optional callback for assistant messages
            (logging, tracing).
    """

    def __init__(
        self,
        *,
        name: str,
        system_prompt: str,
        tools: list[Any],  # claude: ignore
        build_message: Callable[[], str | None],
        start_message: str = "",
        model: str = "claude-sonnet-4-20250514",
        max_thinking_tokens: int | None = None,
        debounce_seconds: float = 3.0,
        builtin_tools: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        on_response: Callable[[AssistantMessage], None] | None = None,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self.build_message = build_message
        self.start_message = start_message or f"[{name} started]"
        self.model = model
        self.max_thinking_tokens = max_thinking_tokens or (128_000 - 1)
        self.debounce_seconds = debounce_seconds
        self.builtin_tools = builtin_tools
        self.allowed_tools = allowed_tools
        self.on_response = on_response

        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event = asyncio.Event()
        self._running = False

    def start(self) -> None:
        """Start the background agent as an asyncio task."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    def wake(self) -> None:
        """Signal that new data is available for processing."""
        self._wake.set()

    async def stop(self) -> None:
        """Cancel the background agent and wait for cleanup."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _message_generator(self) -> AsyncGenerator[dict[str, object], None]:
        """Yield user turns: start message, then build_message on each wake."""
        yield {
            "type": "user",
            "message": {"role": "user", "content": self.start_message},
        }

        while self._running:
            await self._wake.wait()
            self._wake.clear()

            # Debounce: batch rapid events into a single turn
            while True:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.debounce_seconds
                    )
                    self._wake.clear()
                except TimeoutError:
                    break

            content = self.build_message()
            if content is None:
                continue

            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
            }

    async def _run(self) -> None:
        """Create SDK client, connect with message generator, process responses."""
        server = create_sdk_mcp_server(
            name=self.name,
            version="1.0.0",
            tools=self.tools,
        )

        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=self.system_prompt,
            max_thinking_tokens=self.max_thinking_tokens,
            permission_mode="bypassPermissions",
            tools=self.builtin_tools,
            mcp_servers={self.name: server},
            allowed_tools=self.allowed_tools or [],
            extra_args={"no-session-persistence": None},
        )

        try:
            client = ClaudeSDKClient(options=options)
            await client.connect(self._message_generator())
            try:
                async for msg in client.receive_messages():
                    self._handle_response(msg)
            finally:
                await client.disconnect()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background agent '%s' crashed", self.name)

    def _handle_response(self, msg: object) -> None:
        """Route response messages for logging."""
        match msg:
            case AssistantMessage():
                if self.on_response:
                    self.on_response(msg)
            case ResultMessage():
                if msg.is_error:
                    logger.error(
                        "Background agent '%s' error: %s", self.name, msg.result
                    )
