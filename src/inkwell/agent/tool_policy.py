"""Conditional tool availability for inkwell.

Manages which tools are available based on API key presence.
Tools degrade gracefully — missing keys log warnings, don't crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inkwell.agent.config import Settings


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

GOOGLE_DOCS_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__docs__create_doc",
        "mcp__docs__create_tab",
        "mcp__docs__write_tab",
        "mcp__docs__read_tab",
        "mcp__docs__insert_comment",
        "mcp__docs__read_comments",
        "mcp__docs__update_overview",
        "mcp__docs__list_tabs",
    }
)

AUTHOR_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__docs__ask_author",
        "mcp__docs__check_author_feedback",
        "mcp__docs__update_progress",
        "mcp__docs__load_corpus",
        "mcp__docs__analyze_voice",
    }
)

FORMAT_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__format__format_lesswrong",
        "mcp__format__format_twitter",
        "mcp__format__format_blog",
    }
)

EXTRACT_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__extract__extract_conversation",
        "mcp__extract__extract_url",
        "mcp__extract__extract_file",
    }
)

RESEARCH_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__research__exa_search",
        "mcp__research__fetch_url",
        "mcp__research__search_arxiv",
        "mcp__research__fetch_arxiv",
        "mcp__research__fred_search",
        "mcp__research__fred_series",
        "mcp__research__polymarket_search",
        "mcp__research__manifold_search",
        "mcp__research__search_markets",
        "mcp__research__wiki_search",
        "mcp__research__fetch_wikipedia",
    }
)

REALTIME_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__session__sleep",
        "mcp__session__context",
        "mcp__session__reply",
        "mcp__session__meta",
        "mcp__session__remind",
        "mcp__session__notes",
        "mcp__session__ideas",
        "mcp__session__schedule_action",
        "mcp__session__debounce",
    }
)

COMPUTE_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__compute__execute_code",
        "mcp__compute__install_package",
        "mcp__compute__query_research",
        "mcp__compute__query_plan",
    }
)


class ToolPolicy:
    """Centralized policy for tool availability."""

    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings

        excluded: set[str] = set()

        if not settings.google_credentials_path:
            excluded.update(GOOGLE_DOCS_TOOLS)
            excluded.update(AUTHOR_TOOLS)

        if not settings.exa_api_key:
            excluded.add("mcp__research__exa_search")

        if not settings.fred_api_key:
            excluded.add("mcp__research__fred_search")
            excluded.add("mcp__research__fred_series")

        self.excluded_tools: frozenset[str] = frozenset(excluded)

    def get_allowed_tools(self) -> list[str]:
        tools: set[str] = set()
        tools.update(BUILTIN_TOOLS)
        tools.update(GOOGLE_DOCS_TOOLS)
        tools.update(AUTHOR_TOOLS)
        tools.update(FORMAT_TOOLS)
        tools.update(EXTRACT_TOOLS)
        tools.update(RESEARCH_TOOLS)
        tools.update(REALTIME_TOOLS)
        tools -= self.excluded_tools
        return sorted(tools)

    def is_tool_available(self, tool_name: str) -> bool:
        return tool_name not in self.excluded_tools


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
            "mcp__research__fetch_url",
            "mcp__research__wiki_search",
            "mcp__research__fetch_wikipedia",
        }
    )
