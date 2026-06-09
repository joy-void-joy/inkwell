"""Tool allowlists for pipeline stages.

Names the built-in and research MCP tools that research-capable stages
(researcher, section writers, fact checker) may call. Output collector,
note, source, and compute tool names are computed by the pipeline's
server builders — only the shared research surface lives here.
"""

from __future__ import annotations

BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
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
    }
)

RESEARCH_TOOLS: frozenset[str] = frozenset(
    {
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
    }
)


def research_tool_names() -> list[str]:
    return sorted(
        (BUILTIN_TOOLS | RESEARCH_TOOLS) - {"Write", "Task", "TodoRead", "TodoWrite"}
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
