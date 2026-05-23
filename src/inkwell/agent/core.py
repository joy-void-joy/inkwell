"""Main agent orchestration for the inkwell writing pipeline.

Persistent mode: the agent stays alive across the full session using
a sleep/wake loop. After stages that produce visible output (section
drafts, review comments), it sleeps and lets the author read. On wake,
it checks for new author comments and decides whether to continue or adjust.
"""

# claude: ignore
# McpServerConfig is imported from a typed module but used with untyped servers.

import logging
from datetime import datetime
from pathlib import Path
from typing import cast

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ContentBlock,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk import McpServerConfig

from lup.client import ResponseCollector, TokenUsage, query
from lup.history import save_session
from lup.hooks import create_permission_hooks, merge_hooks
from lup.mcp import create_mcp_server, extract_sdk_tools
from lup.metrics import get_metrics_summary, log_metrics_summary, reset_metrics
from lup.notes import NotesConfig, setup_notes
from lup.paths import agent_version
from lup.realtime import (
    ActionCallback,
    Scheduler,
    create_meta_before_sleep_guard,
    create_pending_event_guard,
    create_stop_guard,
)
from lup.reflect import ReflectionGate, create_reflection_gate
from lup.sandbox import Sandbox
from lup.trace import TraceLogger

from inkwell.agent.config import settings
from inkwell.agent.models import AgentSessionResult, WritingOutput
from inkwell.agent.prompts import get_system_prompt
from inkwell.agent.session import WritingContext, WritingSessionState
from inkwell.agent.subagents import get_subagents
from inkwell.agent.tool_policy import ToolPolicy
from inkwell.agent.tools.author import AUTHOR_TOOLS
from inkwell.agent.tools.extract import EXTRACT_TOOLS
from inkwell.agent.tools.google_docs import GOOGLE_DOCS_TOOLS
from inkwell.agent.tools.realtime import create_realtime_tools
from inkwell.agent.tools.reflect import create_reflect_tools
from inkwell.agent.tools.research.arxiv import ARXIV_TOOLS
from inkwell.agent.tools.research.exa import EXA_TOOLS
from inkwell.agent.tools.research.fetch import FETCH_TOOLS
from inkwell.agent.tools.research.fred import FRED_TOOLS
from inkwell.agent.tools.research.markets import MARKET_TOOLS
from inkwell.agent.tools.research.wikipedia import WIKIPEDIA_TOOLS
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
    """Build context state for the realtime context tool.

    Queries the Google Doc for new author comments and returns live
    session state including section status and pending questions.
    """
    new_comments = session_state.get_new_author_comments()
    terminal_messages = session_state.consume_terminal_messages()

    return WritingContext(
        stage=session_state.stage,
        doc_id=session_state.doc_id,
        doc_url=session_state.doc_url,
        sections_status=list(session_state.sections),
        pending_questions=list(session_state.pending_questions),
        new_author_comments=new_comments,
        terminal_messages=terminal_messages,
        scheduler=cast(dict[str, object], scheduler.get_state()),
    )


def build_agent_servers(
    *,
    session_dir: Path,
    outputs_dir: Path | None = None,
    sandbox: Sandbox | None = None,
    gate: ReflectionGate | None = None,
    scheduler: Scheduler | None = None,
    trace_logger: TraceLogger | None = None,
    session_state: WritingSessionState | None = None,
) -> dict[str, McpServerConfig]:
    """Create the agent's MCP servers."""
    all_doc_tools = [*GOOGLE_DOCS_TOOLS, *AUTHOR_TOOLS, *VOICE_TOOLS, *FORMAT_TOOLS]
    docs_server = create_mcp_server(
        name="docs",
        version="1.0.0",
        tools=extract_sdk_tools(all_doc_tools),
    )

    extract_server = create_mcp_server(
        name="extract",
        version="1.0.0",
        tools=extract_sdk_tools(EXTRACT_TOOLS),
    )

    research_tools = [
        *EXA_TOOLS,
        *ARXIV_TOOLS,
        *FETCH_TOOLS,
        *FRED_TOOLS,
        *MARKET_TOOLS,
        *WIKIPEDIA_TOOLS,
    ]
    research_server = create_mcp_server(
        name="research",
        version="1.0.0",
        tools=extract_sdk_tools(research_tools),
    )

    reflect_kit = create_reflect_tools(
        session_dir=session_dir,
        outputs_dir=outputs_dir,
        gate=gate,
    )
    reflect_server = create_mcp_server(
        name="notes",
        version="1.0.0",
        tools=extract_sdk_tools(reflect_kit["tools"]),
    )

    all_servers: list[McpServerConfig] = [
        docs_server,
        extract_server,
        research_server,
        reflect_server,
    ]

    if scheduler is not None:
        state = session_state or WritingSessionState()
        realtime_tools = create_realtime_tools(
            scheduler=scheduler,
            build_context=lambda n: build_context(
                n, scheduler=scheduler, session_state=state
            ),
            trace_logger=trace_logger,
            session_notes=state.notes,
        )
        session_server = create_mcp_server(
            name="session",
            version="1.0.0",
            tools=extract_sdk_tools(realtime_tools),
        )
        all_servers.append(session_server)

    if sandbox is not None:
        all_servers.append(sandbox.create_mcp_server())

    policy = ToolPolicy.from_settings(settings)
    return policy.get_mcp_servers(*all_servers)


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
    gate = ReflectionGate()
    servers = build_agent_servers(
        session_dir=notes_config.session,
        outputs_dir=notes_config.output.parent,
        sandbox=sandbox,
        gate=gate,
        scheduler=scheduler,
        trace_logger=trace_logger,
        session_state=session_state,
    )

    permission_hooks = create_permission_hooks(notes_config.rw, notes_config.ro)

    if persistent and scheduler is not None:
        gate_hooks = create_reflection_gate(
            gate=gate,
            gated_tool="StructuredOutput",
            reflection_tool_name="mcp__notes__review",
        )
        stop_hooks = create_stop_guard()
        meta_hooks = create_meta_before_sleep_guard(
            scheduler=scheduler,
            sleep_tool_name="mcp__session__sleep",
        )
        event_hooks = create_pending_event_guard(
            check_unread=lambda: (session_state or WritingSessionState()).check_unread_comments(),
            scheduler=scheduler,
            guarded_tools=["mcp__session__sleep"],
        )
        hooks = merge_hooks(permission_hooks, gate_hooks)
        hooks = merge_hooks(hooks, stop_hooks)
        hooks = merge_hooks(hooks, meta_hooks)
        hooks = merge_hooks(hooks, event_hooks)
    else:
        gate_hooks = create_reflection_gate(
            gate=gate,
            gated_tool="StructuredOutput",
            reflection_tool_name="mcp__notes__review",
        )
        hooks = merge_hooks(permission_hooks, gate_hooks)

    policy = ToolPolicy.from_settings(settings)

    return ClaudeAgentOptions(
        model=settings.model,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": get_system_prompt(),
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
        agents=get_subagents(),
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
        if isinstance(block, ToolUseBlock) and block.name in (
            "WebSearch",
            "WebFetch",
        ):
            if isinstance(block.input, dict):
                source = block.input.get("url") or block.input.get("query")
                if source:
                    sources.append(str(source))
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


async def run_agent(
    task: str,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    persistent: bool = True,
    on_action: ActionCallback | None = None,
) -> AgentSessionResult:
    """Run the writing agent on a task.

    In persistent mode (default), the agent uses a sleep/wake loop to stay
    alive across the full writing session. The author can follow progress
    in the Google Doc and leave comments at any time.

    Args:
        task: The writing task — a Claude share link, brief, or revision request.
        session_id: Optional session identifier.
        task_id: Optional task identifier.
        persistent: Whether to use sleep/wake persistent mode.
        on_action: Callback for agent actions (used by CLI for terminal output).
    """
    if session_id is None:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("Starting session %s (persistent=%s)", session_id, persistent)
    reset_metrics()

    notes = setup_notes(session_id, task_id or "0")
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

        scheduler = Scheduler(on_action=action_callback)

    with sandbox:
        options = build_options(
            notes,
            sandbox=sandbox,
            scheduler=scheduler,
            trace_logger=trace_logger,
            session_state=session_state,
            persistent=persistent,
        )
        collector = await query(
            task,
            options=options,
            trace_logger=trace_logger,
        )

    trace_logger.save()
    log_metrics_summary()

    session_result = build_result(
        session_id=session_id,
        task_id=task_id,
        collector=collector,
    )

    save_session(session_result, session_id=session_result.session_id)

    return session_result
