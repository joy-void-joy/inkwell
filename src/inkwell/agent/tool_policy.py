"""Tool allowlists for pipeline stages.

Names the built-in and research MCP tools that research-capable stages
(researcher, section writers, fact checker) may call. Output collector,
note, source, and compute tool names are computed by the pipeline's
server builders — only the shared research surface lives here.
"""

from __future__ import annotations

BUILTIN_TOOLS: tuple[str, ...] = (
    "WebSearch",
    "WebFetch",
    "Read",
    "Write",
    "Glob",
    "Grep",
    "Bash",
    "Task",
    "TodoRead",
    "TodoWrite",
)
"""The built-in tools a stage of this pipeline may be granted."""

RESEARCH_TOOLS: tuple[str, ...] = (
    "mcp__research__exa_search",
    "mcp__research__search_arxiv",
    "mcp__research__fetch_arxiv",
    "mcp__research__fred_search",
    "mcp__research__fred_series",
    "mcp__research__polymarket_search",
    "mcp__research__manifold_search",
    "mcp__research__search_markets",
    "mcp__research__polymarket_price",
    "mcp__research__wiki_search",
    "mcp__research__fetch_wikipedia",
)
"""The research MCP tools the research-capable stages share."""

WITHHELD_FROM_RESEARCH: tuple[str, ...] = ("Write", "Task", "TodoRead", "TodoWrite")
"""Built-ins a research stage is not granted: it reads and reports, it does
not write files of its own or spawn work beside itself."""


def research_tool_names() -> list[str]:
    return sorted(
        {
            name
            for name in (*BUILTIN_TOOLS, *RESEARCH_TOOLS)
            if name not in WITHHELD_FROM_RESEARCH
        }
    )


def review_tool_names() -> list[str]:
    return sorted(
        {
            "WebSearch",
            "WebFetch",
            "Read",
            "Glob",
            "Grep",
            "mcp__research__exa_search",
            "mcp__research__wiki_search",
            "mcp__research__fetch_wikipedia",
        }
    )
