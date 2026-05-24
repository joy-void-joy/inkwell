"""Main agent orchestration for the inkwell writing pipeline.

Persistent mode: the agent stays alive across the full session using
a sleep/wake loop. After stages that produce visible output (section
drafts, review comments), it sleeps and lets the author read. On wake,
it checks for new author comments and decides whether to continue or adjust.

Exports:
- setup_session() — create all infrastructure for a writing session
- run_batch() — run the pipeline non-interactively
- run_agent() — one-shot freeform execution (no pipeline)
- build_options() — build ClaudeAgentOptions (used by devtools REPL)
- build_agent_servers() — create MCP servers (used by devtools REPL)
- build_result() — convert a ResponseCollector into AgentSessionResult
"""

# claude: ignore
# McpServerConfig is imported from a typed module but used with untyped servers.

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ContentBlock,
    HookInput,
    HookMatcher,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk import McpServerConfig
from claude_agent_sdk.types import HookContext, HookEvent, SyncHookJSONOutput

from lup.client import CostAccumulator, ResponseCollector, TokenUsage, query
from lup.history import save_session
from lup.hooks import (
    HooksConfig,
    block_hook_output,
    create_permission_hooks,
    merge_hooks,
)
from lup.mcp import create_mcp_server, extract_sdk_tools
from lup.metrics import get_metrics_summary, log_metrics_summary, reset_metrics
from lup.notes import NotesConfig, setup_notes
from lup.paths import agent_version
from lup.realtime import (
    ActionCallback,
    Scheduler,
    create_meta_before_sleep_guard,
    create_pending_event_guard,
)
from lup.sandbox import Sandbox
from lup.trace import TraceLogger

from inkwell.agent.config import settings
from inkwell.agent.models import AgentSessionResult, WritingOutput
from inkwell.agent.pipeline import PipelineListener, run_pipeline
from inkwell.agent.prompts import get_system_prompt
from inkwell.agent.session import WritingContext, WritingSessionState
from inkwell.agent.tool_policy import ToolPolicy
from inkwell.agent.tools.author import AUTHOR_TOOLS
from inkwell.agent.tools.extract import EXTRACT_TOOLS
from inkwell.agent.tools.google_docs import GOOGLE_DOCS_TOOLS, configure_session_state
from inkwell.agent.tools.realtime import create_realtime_tools
from inkwell.agent.pipeline import build_research_tools
from inkwell.agent.tools.formats import FORMAT_TOOLS
from inkwell.agent.tools.voice import VOICE_TOOLS

logger = logging.getLogger(__name__)

NOTES_PATH = Path(settings.notes_path)
TRACES_PATH = NOTES_PATH / "traces"


def build_context(
    last_events: int,
    *,
    scheduler: Scheduler,
    session_state: WritingSessionState,
) -> WritingContext:
    """Build context state for the realtime context tool."""
    new_comments = session_state.get_new_author_comments()

    return WritingContext(
        stage=session_state.stage,
        doc_id=session_state.doc_id,
        doc_url=session_state.doc_url,
        sections_status=list(session_state.sections),
        pending_questions=list(session_state.pending_questions),
        new_author_comments=new_comments,
        scheduler=cast(dict[str, object], scheduler.get_state()),
    )


def create_writing_stop_guard(
    session_state: WritingSessionState,
) -> HooksConfig:
    """Stop guard that adapts to pipeline state."""

    async def stop_guard(
        input_data: HookInput,
        _tool_use_id: str | None,  # claude: ignore
        _context: HookContext,  # claude: ignore
    ) -> SyncHookJSONOutput:
        if input_data["hook_event_name"] != "Stop":
            return SyncHookJSONOutput()
        if input_data["stop_hook_active"]:
            return SyncHookJSONOutput()
        if not session_state.doc_id:
            return SyncHookJSONOutput()
        return block_hook_output(
            "Use sleep to enter standby mode. The author may have "
            "feedback or revision requests at any time."
        )

    key: HookEvent = "Stop"
    return {key: [HookMatcher(hooks=[stop_guard])]}


class SessionSetup(NamedTuple):
    notes: NotesConfig
    trace_logger: TraceLogger
    sandbox: Sandbox
    session_state: WritingSessionState
    scheduler: Scheduler | None
    options: ClaudeAgentOptions


def setup_session(
    session_id: str,
    *,
    persistent: bool = True,
    on_action: ActionCallback | None = None,
    on_sleep: Callable[[], None] | None = None,
) -> SessionSetup:
    """Create all infrastructure for a writing session.

    Returns a SessionSetup with notes, trace, sandbox, session state,
    scheduler, and pre-built ClaudeAgentOptions. Used by run_agent()
    (freeform mode) and as infrastructure for the pipeline.
    """
    notes = setup_notes(session_id, "0")
    trace_path = TRACES_PATH / session_id / f"{datetime.now().strftime('%H%M%S')}.md"
    trace_logger = TraceLogger(trace_path=trace_path, title=f"Session {session_id}")

    sandbox = Sandbox(
        session_id=session_id,
        shared_dir=notes.session / "sandbox_shared",
        timeout_seconds=settings.sandbox_timeout_seconds,
    )

    session_state = WritingSessionState()

    scheduler: Scheduler | None = None
    if persistent:

        async def action_callback(content: str) -> None:
            logger.info("Agent action: %s", content[:100])
            if on_action:
                await on_action(content)

        def sleep_callback() -> None:
            session_state.sleep_entered.set()
            if on_sleep:
                on_sleep()

        scheduler = Scheduler(on_action=action_callback, on_sleep=sleep_callback)

    options = build_options(
        notes,
        sandbox=sandbox,
        scheduler=scheduler,
        trace_logger=trace_logger,
        session_state=session_state,
        persistent=persistent,
    )

    return SessionSetup(
        notes=notes,
        trace_logger=trace_logger,
        sandbox=sandbox,
        session_state=session_state,
        scheduler=scheduler,
        options=options,
    )


def build_agent_servers(
    *,
    session_dir: Path,
    sandbox: Sandbox | None = None,
    scheduler: Scheduler | None = None,
    trace_logger: TraceLogger | None = None,
    session_state: WritingSessionState | None = None,
    policy: ToolPolicy | None = None,
) -> dict[str, McpServerConfig]:
    """Create the agent's MCP servers."""
    if session_state is not None:
        configure_session_state(session_state)

    docs_server = create_mcp_server(
        name="docs",
        version="1.0.0",
        tools=extract_sdk_tools([*GOOGLE_DOCS_TOOLS, *AUTHOR_TOOLS, *VOICE_TOOLS]),
    )

    extract_server = create_mcp_server(
        name="extract",
        version="1.0.0",
        tools=extract_sdk_tools(EXTRACT_TOOLS),
    )

    research_tools = build_research_tools()
    research_server = create_mcp_server(
        name="research",
        version="1.0.0",
        tools=extract_sdk_tools(research_tools),
    )

    format_server = create_mcp_server(
        name="format",
        version="1.0.0",
        tools=extract_sdk_tools(FORMAT_TOOLS),
    )

    all_servers: list[McpServerConfig] = [
        docs_server,
        extract_server,
        research_server,
        format_server,
    ]

    if scheduler is not None:
        state = session_state or WritingSessionState()
        realtime_tools = create_realtime_tools(
            scheduler=scheduler,
            build_context=lambda n: build_context(
                n, scheduler=scheduler, session_state=state
            ),
            trace_logger=trace_logger,
        )
        session_server = create_mcp_server(
            name="session",
            version="1.0.0",
            tools=extract_sdk_tools(realtime_tools),
        )
        all_servers.append(session_server)

    if sandbox is not None:
        all_servers.append(sandbox.create_mcp_server())

    resolved_policy = policy or ToolPolicy.from_settings(settings)
    return resolved_policy.get_mcp_servers(*all_servers)


def build_options(
    notes_config: NotesConfig,
    *,
    sandbox: Sandbox | None = None,
    scheduler: Scheduler | None = None,
    trace_logger: TraceLogger | None = None,
    session_state: WritingSessionState | None = None,
    persistent: bool = True,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for the writing agent."""
    policy = ToolPolicy.from_settings(settings)

    servers = build_agent_servers(
        session_dir=notes_config.session,
        sandbox=sandbox,
        scheduler=scheduler,
        trace_logger=trace_logger,
        session_state=session_state,
        policy=policy,
    )

    hooks = create_permission_hooks(notes_config.rw, notes_config.ro)

    if persistent and scheduler is not None:
        state = session_state or WritingSessionState()
        for layer in [
            create_writing_stop_guard(state),
            create_meta_before_sleep_guard(
                scheduler=scheduler,
                sleep_tool_name="mcp__session__sleep",
            ),
            create_pending_event_guard(
                check_unread=state.check_unread_comments,
                scheduler=scheduler,
                guarded_tools=["mcp__session__sleep"],
            ),
        ]:
            hooks = merge_hooks(hooks, layer)

    return ClaudeAgentOptions(
        model=settings.model,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": get_system_prompt(author_email=settings.author_email),
        },
        max_thinking_tokens=settings.max_thinking_tokens or (128_000 - 1),
        permission_mode="bypassPermissions",
        extra_args={"no-session-persistence": None},
        hooks=hooks,
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        },
        mcp_servers=servers,
        add_dirs=[str(d) for d in notes_config.all_dirs],
        allowed_tools=policy.get_allowed_tools(),
        output_format={
            "type": "json_schema",
            "schema": WritingOutput.model_json_schema(),
        },
    )


def extract_sources(blocks: list[ContentBlock]) -> list[str]:
    sources: list[str] = []
    for block in blocks:
        if not isinstance(block, ToolUseBlock):
            continue
        if block.name not in ("WebSearch", "WebFetch"):
            continue
        tool_input = block.input
        for key in ("url", "query"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                sources.append(value)
                break
    return sources


def build_result(
    *,
    session_id: str,
    task_id: str | None,
    collector: ResponseCollector,
) -> AgentSessionResult:
    result = collector.result
    if result is None:
        raise RuntimeError("No result in collector")

    output = WritingOutput(
        title="Untitled",
        content="",
        google_doc_url="",
        word_count=0,
        sections_completed=0,
        summary="No output produced",
    )
    if result.structured_output:
        output = WritingOutput.model_validate(result.structured_output)

    return AgentSessionResult(
        session_id=session_id,
        task_id=task_id,
        agent_version=agent_version(),
        timestamp=datetime.now().isoformat(),
        output=output,
        reasoning="".join(b.text for b in collector.blocks if isinstance(b, TextBlock)),
        sources_consulted=extract_sources(collector.blocks),
        duration_seconds=(result.duration_ms / 1000) if result.duration_ms else None,
        cost_usd=result.total_cost_usd,
        token_usage=cast(TokenUsage, result.usage) if result.usage else None,
        tool_metrics=get_metrics_summary(),
    )


async def run_batch(
    source: str,
    *,
    target_format: str = "lesswrong",
    existing_doc_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    refs: list[str] | None = None,
    listener: PipelineListener | None = None,
    on_action: ActionCallback | None = None,
) -> AgentSessionResult:
    """Run the writing pipeline.

    This is the primary entry point for both batch and interactive modes.
    The listener controls progress display and feedback collection.

    Args:
        source: Claude share link, URL, or file path.
        target_format: Output format (lesswrong, twitter, blog).
        session_id: Optional session identifier.
        task_id: Optional task identifier.
        refs: Additional reference URLs or file paths.
        listener: Pipeline event listener (default logs to stdout).
        on_action: Callback for agent actions (used by CLI for terminal output).
    """
    if session_id is None:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("Starting pipeline session %s", session_id)
    reset_metrics()

    setup = setup_session(
        session_id,
        persistent=False,
        on_action=on_action,
    )

    cost_acc = CostAccumulator()

    output = await run_pipeline(
        source,
        target_format=target_format,
        existing_doc_id=existing_doc_id,
        session_state=setup.session_state,
        trace_logger=setup.trace_logger,
        refs=refs,
        listener=listener,
        max_budget_usd=settings.max_budget_usd,
        cost_accumulator=cost_acc,
    )

    setup.trace_logger.save()
    log_metrics_summary()

    result = AgentSessionResult(
        session_id=session_id,
        task_id=task_id,
        agent_version=agent_version(),
        timestamp=datetime.now().isoformat(),
        output=output,
        reasoning="",
        sources_consulted=[],
        duration_seconds=cost_acc.duration_seconds if cost_acc.call_count else None,
        cost_usd=cost_acc.total_cost_usd if cost_acc.call_count else None,
        token_usage=TokenUsage(
            input_tokens=cost_acc.total_input_tokens,
            output_tokens=cost_acc.total_output_tokens,
        )
        if cost_acc.call_count
        else None,
        tool_metrics=get_metrics_summary(),
    )

    save_session(result, session_id=result.session_id)
    return result


async def run_agent(
    task: str,
    *,
    existing_doc_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    persistent: bool = True,
    on_action: ActionCallback | None = None,
) -> AgentSessionResult:
    """Run the writing agent as a freeform LLM session.

    For the structured pipeline, use run_batch() instead.
    This mode gives the agent all tools and a system prompt, then
    lets it work freely — useful for open-ended tasks that don't
    follow the extract-plan-research-write pipeline.

    Args:
        task: The writing task — a brief, revision request, or freeform instruction.
        session_id: Optional session identifier.
        task_id: Optional task identifier.
        persistent: Whether to use sleep/wake persistent mode.
        on_action: Callback for agent actions (used by CLI for terminal output).
    """
    if session_id is None:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("Starting freeform session %s (persistent=%s)", session_id, persistent)
    reset_metrics()

    setup = setup_session(
        session_id,
        persistent=persistent,
        on_action=on_action,
    )

    if existing_doc_id:
        doc_url = f"https://docs.google.com/document/d/{existing_doc_id}/edit"
        setup.session_state.set_doc(existing_doc_id, doc_url)

    with setup.sandbox:
        collector = await query(
            task,
            options=setup.options,
            trace_logger=setup.trace_logger,
        )

    setup.trace_logger.save()
    log_metrics_summary()

    session_result = build_result(
        session_id=session_id,
        task_id=task_id,
        collector=collector,
    )

    save_session(session_result, session_id=session_result.session_id)

    return session_result
