"""Main agent orchestration.

This is a TEMPLATE. Customize for your domain.

Key patterns:
1. Use ClaudeAgentOptions with structured output
2. Create MCP servers with create_mcp_server()
3. Track tool metrics with the @tracked decorator
4. Save sessions for feedback loop analysis
5. Use hooks for permission control
"""

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

from claude_agent_sdk.types import McpServerConfig, McpSdkServerConfig

from lup.client import ResponseCollector, TokenUsage, query
from lup.history import save_session
from lup.hooks import create_permission_hooks, merge_hooks
from lup.mcp import create_mcp_server, extract_sdk_tools
from lup.metrics import get_metrics_summary, log_metrics_summary, reset_metrics
from lup.notes import NotesConfig, setup_notes
from lup.paths import agent_version
from lup.reflect import ReflectionGate, create_reflection_gate
from lup.sandbox import Sandbox
from lup.trace import TraceLogger

from lup_template.agent.config import settings
from lup_template.agent.models import AgentOutput, AgentSessionResult
from lup_template.agent.prompts import get_system_prompt
from lup_template.agent.subagents import get_subagents
from lup_template.agent.tool_policy import ToolPolicy
from lup_template.agent.tools.example import EXAMPLE_TOOLS
from lup_template.agent.tools.reflect import create_reflect_tools

logger = logging.getLogger(__name__)

NOTES_PATH = Path(settings.notes_path)
TRACES_PATH = NOTES_PATH / "traces"


def build_agent_servers(
    *,
    session_dir: Path,
    outputs_dir: Path | None = None,
    sandbox: Sandbox | None = None,
    gate: ReflectionGate | None = None,
) -> dict[str, McpServerConfig]:
    """Create the agent's core MCP servers, passed through ToolPolicy.

    Creates example, notes (reflect), and optionally sandbox servers,
    then applies ToolPolicy filtering.

    Args:
        session_dir: Directory for reflection tool output.
        outputs_dir: Past outputs for reviewer calibration.
        sandbox: Initialized sandbox instance (must be entered as
            context manager by the caller before calling this).
        gate: External ReflectionGate for the reflect tools to use.
            If None, a new gate is created internally.
    """
    example_server = create_mcp_server(
        name="example",
        version="1.0.0",
        tools=extract_sdk_tools(EXAMPLE_TOOLS),
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

    all_servers: list[McpSdkServerConfig] = [example_server, reflect_server]
    if sandbox is not None:
        all_servers.append(sandbox.create_mcp_server())

    policy = ToolPolicy.from_settings(settings)
    return policy.get_mcp_servers(*all_servers)


def build_options(
    notes_config: NotesConfig,
    *,
    sandbox: Sandbox | None = None,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions from settings and notes config.

    Separated from run_agent() so the option-building logic can be
    tested and customized independently.
    """
    gate = ReflectionGate()
    servers = build_agent_servers(
        session_dir=notes_config.session,
        outputs_dir=notes_config.output.parent,
        sandbox=sandbox,
        gate=gate,
    )

    permission_hooks = create_permission_hooks(notes_config.rw, notes_config.ro)

    # Reflection gate: deny StructuredOutput until agent calls review
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
            "schema": AgentOutput.model_json_schema(),
        },
    )


def extract_sources(blocks: list[ContentBlock]) -> list[str]:
    """Extract source URLs/queries from tool use blocks."""
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
    """Build a AgentSessionResult from the completed agent run."""
    result = collector.result
    if result is None:
        raise RuntimeError("No result in collector")

    output = AgentOutput(summary="No output produced", factors=[], confidence=0.5)
    if result.structured_output:
        output = AgentOutput.model_validate(result.structured_output)

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
) -> AgentSessionResult:
    """Run the agent on a task.

    Args:
        task: The task/prompt for the agent.
        session_id: Unique identifier for this session. Auto-generated if None.
        task_id: Optional task identifier (e.g., question ID for forecasting).

    Returns:
        AgentSessionResult with the agent's output and metadata.
    """
    if session_id is None:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("Starting session %s", session_id)
    reset_metrics()

    notes = setup_notes(session_id, task_id or "0")
    trace_path = TRACES_PATH / session_id / f"{datetime.now().strftime('%H%M%S')}.md"
    trace_logger = TraceLogger(trace_path=trace_path, title=f"Session {session_id}")

    # --- Sandbox (optional, requires Docker) ---
    # Customize: adjust pre_install, timeout, network_mode, or remove entirely
    # if your agent doesn't need code execution.
    sandbox = Sandbox(
        session_id=session_id,
        shared_dir=notes.session / "sandbox_shared",
        timeout_seconds=settings.sandbox_timeout_seconds,
    )

    with sandbox:
        options = build_options(notes, sandbox=sandbox)
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
