"""Lup — agent development library for the Claude Agent SDK.

Core utilities for building agents with hooks, MCP tools, tracing, and session management.
"""

from lup.client import ResponseCollector, TokenUsage, build_client, query
from lup.hooks import (
    HooksConfig,
    allow_hook_output,
    block_hook_output,
    create_nudge_hook,
    create_permission_hooks,
    deny_hook_output,
    merge_hooks,
)
from lup.mcp import (
    LupMcpTool,
    ToolError,
    create_mcp_server,
    extract_sdk_tools,
    lup_tool,
)
from lup.metrics import (
    MetricsSummary,
    get_metrics_summary,
    log_metrics_summary,
    reset_metrics,
)
from lup.paths import agent_version, configure, project_root
from lup.reflect import ReflectionGate, create_reflection_gate
from lup.trace import TraceLogger, print_message

__all__ = [
    "HooksConfig",
    "LupMcpTool",
    "MetricsSummary",
    "ReflectionGate",
    "ResponseCollector",
    "TokenUsage",
    "ToolError",
    "TraceLogger",
    "agent_version",
    "allow_hook_output",
    "block_hook_output",
    "build_client",
    "configure",
    "create_mcp_server",
    "create_nudge_hook",
    "create_permission_hooks",
    "create_reflection_gate",
    "deny_hook_output",
    "extract_sdk_tools",
    "get_metrics_summary",
    "log_metrics_summary",
    "lup_tool",
    "merge_hooks",
    "print_message",
    "project_root",
    "query",
    "reset_metrics",
]
