"""Example MCP tools showing the pattern.

This is a TEMPLATE. Create your own tools following this pattern.

Key patterns:
1. Use @lup_tool(description, InputModel, OutputModel) decorator
2. Define input/output schemas as BaseModel with Field(description=...)
3. Handler receives a validated model instance (not a raw dict)
4. Handler must return a BaseModel instance (auto-serialized to MCP response)
5. Tool name defaults to the function name; override with name="..."
6. Metrics (duration, errors) are tracked automatically

Tool descriptions are the agent's only documentation for each tool.
A terse description forces the agent to guess when/why to use a tool,
which leads to misuse or underuse. A good description answers:
  - WHAT: What does this tool do? (concrete behavior)
  - WHEN: When should the agent use it? (triggers, conditions)
  - WHY: Why does this tool exist? (what problem it solves)
This keeps tool knowledge in the tool itself rather than in the prompt,
so descriptions stay accurate as tools are added or changed.
"""

from pydantic import BaseModel, Field

from lup.mcp import ToolError, lup_tool


# --- Schemas ---
# Define as BaseModel with Field(description=...) for validation + rich JSON Schema


class SearchInput(BaseModel):
    """Input for the search tool."""

    query: str = Field(description="Search query string")
    limit: int = Field(default=10, description="Maximum number of results to return")


class SearchResult(BaseModel):
    """A single search result."""

    title: str = Field(description="Title of the result")
    url: str = Field(description="URL of the result")


class SearchOutput(BaseModel):
    """Output from the search tool."""

    query: str = Field(description="The query that was searched")
    results: list[SearchResult] = Field(description="Matching search results")
    count: int = Field(description="Total number of results")


class FetchInput(BaseModel):
    """Input for the fetch tool."""

    url: str = Field(description="URL to fetch content from")


class FetchOutput(BaseModel):
    """Output from the fetch tool."""

    url: str = Field(description="The URL that was fetched")
    content: str = Field(description="Page content (may be truncated)")
    status: int = Field(description="HTTP status code")


# --- Tool Implementations ---


@lup_tool(
    "Search for information using keyword queries. "
    "Use this when the agent needs to find data that isn't available in local notes "
    "or when exploring a topic before making decisions. "
    "Exists because the agent has no built-in knowledge beyond its training data. "
    "Returns a JSON object with {query, results: [{title, url}], count}. "
    "Replace this with your actual search implementation."
)
async def search_example(params: SearchInput) -> SearchOutput:
    """Search for information."""

    if not params.query:
        raise ToolError("Query is required")

    # TODO: Implement actual search logic
    # Example with a real search API:
    #
    # try:
    #     results = await search_api.search(params.query, limit=params.limit)
    #     return SearchOutput(query=params.query, results=results, count=len(results))
    # except Exception as e:
    #     raise ToolError(f"Search failed: {e}") from e

    # Placeholder response
    return SearchOutput(
        query=params.query,
        results=[
            SearchResult(title="Example Result 1", url="https://example.com/1"),
            SearchResult(title="Example Result 2", url="https://example.com/2"),
        ],
        count=2,
    )


@lup_tool(
    "Fetch the full content of a web page by URL. "
    "Use this when the agent has a specific URL to retrieve — e.g., from search "
    "results, a known reference, or a link found in notes. "
    "Exists because the agent cannot browse the web directly; this tool provides "
    "read access to individual pages. "
    "Returns a JSON object with {url, content, status}. "
    "Replace this with your actual fetch implementation."
)
async def fetch_example(params: FetchInput) -> FetchOutput:
    """Fetch content from a URL."""

    if not params.url:
        raise ToolError("URL is required")

    # TODO: Implement actual fetch logic
    # Example with httpx:
    #
    # try:
    #     async with httpx.AsyncClient() as client:
    #         response = await client.get(params.url)
    #         response.raise_for_status()
    #         return FetchOutput(url=params.url, content=response.text[:5000], status=200)
    # except Exception as e:
    #     raise ToolError(f"Fetch failed: {e}") from e

    # Placeholder response
    return FetchOutput(
        url=params.url,
        content="Example content from the URL",
        status=200,
    )


# --- Tool Collection ---
# Group tools for your MCP server

EXAMPLE_TOOLS = [
    search_example,
    fetch_example,
]
"""List of example tools for the example MCP server."""
