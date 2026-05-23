"""Wikipedia article retrieval.

Fetches Wikipedia articles with clean text extraction. Useful for
background context, definitions, and historical information.
"""

import logging

import httpx
from pydantic import BaseModel, Field

from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/api/rest_v1"
WIKI_ACTION_API = "https://en.wikipedia.org/w/api.php"


class WikiSearchResult(BaseModel):
    title: str = Field(description="Article title")
    description: str = Field(default="", description="Short description")
    url: str = Field(description="Wikipedia URL")


class WikiSearchInput(BaseModel):
    query: str = Field(description="Search query")
    limit: int = Field(default=5, ge=1, le=20, description="Max results")


class WikiSearchOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[WikiSearchResult] = Field(description="Matching articles")
    count: int = Field(description="Number of results")


class FetchWikipediaInput(BaseModel):
    title: str = Field(
        description=(
            "Wikipedia article title (e.g. 'Prediction_market', 'GPT-4'). "
            "Use wiki_search first if unsure of the exact title."
        )
    )
    section: str | None = Field(
        default=None,
        description="Specific section title to extract (returns full article if omitted)",
    )


class FetchWikipediaOutput(BaseModel):
    title: str = Field(description="Article title")
    url: str = Field(description="Wikipedia URL")
    content: str = Field(description="Article text content")
    word_count: int = Field(description="Approximate word count")


@lup_tool(
    "Search Wikipedia for articles by keyword. Returns titles, short "
    "descriptions, and URLs. Use this to find the right article title "
    "before calling fetch_wikipedia to read the full text."
)
async def wiki_search(params: WikiSearchInput) -> WikiSearchOutput:
    query_params = {
        "action": "query",
        "list": "search",
        "srsearch": params.query,
        "srlimit": params.limit,
        "format": "json",
        "srprop": "snippet",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(WIKI_ACTION_API, params=query_params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError(f"Wikipedia API error: {e}") from e
        data = resp.json()

    results: list[WikiSearchResult] = []
    for item in data.get("query", {}).get("search", []):
        title = item.get("title", "")
        safe_title = title.replace(" ", "_")
        results.append(
            WikiSearchResult(
                title=title,
                description=strip_html(item.get("snippet", "")),
                url=f"https://en.wikipedia.org/wiki/{safe_title}",
            )
        )

    return WikiSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Fetch a Wikipedia article's full text. Returns clean, readable text "
    "extracted from the article. Use wiki_search first to find the right "
    "title. Optionally extract just one section by name. Good for "
    "background context, definitions, historical timelines, and "
    "verifying basic facts."
)
async def fetch_wikipedia(params: FetchWikipediaInput) -> FetchWikipediaOutput:
    safe_title = params.title.replace(" ", "_")

    query_params: dict[str, str | int] = {
        "action": "query",
        "titles": params.title,
        "prop": "extracts",
        "explaintext": "1",
        "format": "json",
    }
    if params.section is None:
        query_params["exlimit"] = 1

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(WIKI_ACTION_API, params=query_params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError(f"Wikipedia API error: {e}") from e
        data = resp.json()

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise ToolError(f"No Wikipedia article found for '{params.title}'.")

    page = next(iter(pages.values()))
    if page.get("missing") is not None:
        raise ToolError(
            f"Wikipedia article '{params.title}' not found. "
            "Use wiki_search to find the correct title."
        )

    content = page.get("extract", "")
    if not content:
        raise ToolError(f"No text content in Wikipedia article '{params.title}'.")

    if params.section:
        section_content = extract_section(content, params.section)
        if section_content:
            content = section_content

    content = content[:30000]

    return FetchWikipediaOutput(
        title=page.get("title", params.title),
        url=f"https://en.wikipedia.org/wiki/{safe_title}",
        content=content,
        word_count=len(content.split()),
    )


def extract_section(text: str, section_title: str) -> str | None:
    """Extract a specific section from Wikipedia plaintext."""
    lines = text.split("\n")
    target = section_title.lower().strip()
    capturing = False
    captured: list[str] = []

    for line in lines:
        stripped = line.strip()
        heading = stripped.lstrip("=").rstrip("=").strip().lower()

        if heading == target:
            capturing = True
            captured.append(line)
            continue

        if capturing:
            if stripped.startswith("==") and heading != target:
                break
            captured.append(line)

    return "\n".join(captured) if captured else None


def strip_html(text: str) -> str:
    """Remove HTML tags from a snippet."""
    import re

    return re.sub(r"<[^>]+>", "", text)


WIKIPEDIA_TOOLS = [wiki_search, fetch_wikipedia]
