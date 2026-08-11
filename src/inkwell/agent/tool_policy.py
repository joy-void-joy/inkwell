"""Tool allowlists for pipeline stages.

Names the built-in and research MCP tools that research-capable stages
(researcher, section writers, fact checker) may call. Output collector,
note, source, and compute tool names are computed by the pipeline's
server builders — only the shared research surface lives here.

That surface is *derived* from the tool declarations rather than restated. A
name written out here as well as in the server would be two lists to keep in
step, and a tool missing from one of them is registered but uncallable — which
looks to a stage exactly like a tool that does not work.
"""

from __future__ import annotations

from inkwell.agent.tools.research.arxiv import ARXIV_TOOLS
from inkwell.agent.tools.research.corpus import CORPUS_TOOLS
from inkwell.agent.tools.research.exa import EXA_TOOLS
from inkwell.agent.tools.research.fred import FRED_TOOLS
from inkwell.agent.tools.research.markets import MARKET_TOOLS
from inkwell.agent.tools.research.wikipedia import WIKIPEDIA_TOOLS
from lup.mcp import LupMcpTool

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

RESEARCH_SERVER = "research"
"""The MCP server every research tool is mounted under."""

RESEARCH_TOOLSET: tuple[LupMcpTool, ...] = (
    *CORPUS_TOOLS,
    *EXA_TOOLS,
    *ARXIV_TOOLS,
    *FRED_TOOLS,
    *MARKET_TOOLS,
    *WIKIPEDIA_TOOLS,
)
"""Every research tool, the corpus first: what is already gathered comes before
what has to be fetched. The server mounts this list and the allowlist below is
read off it, so adding a tool to one of the groups reaches a stage."""


def research_name(tool: LupMcpTool) -> str:
    """One research tool's name as an allowlist entry spells it."""
    return f"mcp__{RESEARCH_SERVER}__{tool.name}"


RESEARCH_TOOLS: tuple[str, ...] = tuple(
    research_name(tool) for tool in RESEARCH_TOOLSET
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
            "mcp__research__corpus_overview",
            "mcp__research__search_corpus",
            "mcp__research__corpus_titles",
        }
    )
