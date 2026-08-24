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

CORPUS_TOOLS: tuple[str, ...] = (
    "mcp__research__corpus_overview",
    "mcp__research__corpus_search",
)
"""Asking the corpus a question, rather than reading what it was handed.

Named apart from the rest of research because the stages that need it are not
only the ones that gather. A stage deciding what a piece establishes plans
around whatever its briefing happened to carry: it is told how many documents
matched and given no way to reach the ones below the cap, so a subject the
first page missed reads as a subject the corpus does not hold.
"""

RESEARCH_TOOLS: tuple[str, ...] = (
    *CORPUS_TOOLS,
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
    "mcp__research__top_up_corpus",
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


def planning_tool_names() -> list[str]:
    """What a stage that decides a piece's structure may call.

    The corpus and the files it is pointed at, and nothing that gathers from
    outside — a planner is choosing among evidence, not collecting it, and the
    stage that collects runs after this one with the whole research surface.
    """
    return sorted({"Read", "Glob", "Grep", *CORPUS_TOOLS})


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
            *CORPUS_TOOLS,
        }
    )
