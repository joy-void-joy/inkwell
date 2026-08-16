"""Wikipedia article retrieval.

Fetches Wikipedia articles with clean text extraction. Useful for
background context, definitions, and historical information.
"""

import logging
from collections.abc import Iterator
from html.parser import HTMLParser
from urllib.parse import quote

import httpx
from httpx import QueryParams
from pydantic import BaseModel, ConfigDict, Field, computed_field

from lup.workspace.content_safety import SavedContent, save_content
from lup.mcp import ToolError, lup_tool

from inkwell.agent.provenance import Acquisition

logger = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/api/rest_v1"
WIKI_ACTION_API = "https://en.wikipedia.org/w/api.php"


def article_url(title: str) -> str:
    """Where an article by that title is read."""
    return f"https://en.wikipedia.org/wiki/{quote(title.strip(), safe='')}"


class WireSearchItem(BaseModel):
    """One search hit, by the names the action API sends."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str = ""
    snippet: str = ""


class WireSearchQuery(BaseModel):
    """The search half of an action-API response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    search: list[WireSearchItem] = Field(default_factory=list)


class WireSearchResponse(BaseModel):
    """What a search request answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    query: WireSearchQuery = WireSearchQuery()


class WirePage(BaseModel):
    """One article page, which the API reports as missing by including a key."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str = ""
    extract: str = ""
    missing: str | None = None


class WirePageQuery(BaseModel):
    """The pages half of an action-API response, keyed by page id."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    pages: dict[str, WirePage] = Field(default_factory=dict)


class WirePageResponse(BaseModel):
    """What an extract request answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    query: WirePageQuery = WirePageQuery()


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
    content: SavedContent = Field(description="Article text saved to disk")
    section_extracted: bool = Field(
        default=False, description="True if a specific section was extracted"
    )

    @computed_field
    @property
    def acquisition(self) -> Acquisition:
        """How this article was acquired — copy it into record_finding as it stands.

        An encyclopedia article is background rather than a primary source, and
        it carries no publication date, so record_finding asks for the date the
        revision was read, or 'undated'.
        """
        return Acquisition(path="wikipedia", url=self.url)


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
        found = WireSearchResponse.model_validate(resp.json())

    results = [
        WikiSearchResult(
            title=item.title,
            description=strip_html(item.snippet),
            url=article_url(item.title),
        )
        for item in found.query.search
    ]

    return WikiSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Fetch a Wikipedia article's full text. Saves the extracted text to "
    "disk and returns a file path, word count, and preview. Use Read "
    "with offset/limit to access the full text. Use wiki_search first "
    "to find the right title. Optionally extract just one section by "
    "name. Good for background context, definitions, historical "
    "timelines, and verifying basic facts."
)
async def fetch_wikipedia(params: FetchWikipediaInput) -> FetchWikipediaOutput:
    query_params = QueryParams(
        {
            "action": "query",
            "titles": params.title,
            "prop": "extracts",
            "explaintext": "1",
            "format": "json",
            **({"exlimit": 1} if params.section is None else {}),
        }
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(WIKI_ACTION_API, params=query_params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError(f"Wikipedia API error: {e}") from e
        fetched = WirePageResponse.model_validate(resp.json())

    if not fetched.query.pages:
        raise ToolError(f"No Wikipedia article found for '{params.title}'.")

    page = next(iter(fetched.query.pages.values()))
    if page.missing is not None:
        raise ToolError(
            f"Wikipedia article '{params.title}' not found. "
            "Use wiki_search to find the correct title."
        )

    if not page.extract:
        raise ToolError(f"No text content in Wikipedia article '{params.title}'.")

    section = extract_section(page.extract, params.section) if params.section else None
    content = section if section else page.extract

    title = page.title or params.title
    saved = save_content("wikipedia", title, content)

    return FetchWikipediaOutput(
        title=title,
        url=article_url(params.title),
        content=saved,
        section_extracted=section is not None,
    )


HEADING_MARK = "=="


def heading_title(line: str) -> str | None:
    """The title a plaintext heading line names, or nothing if it is prose.

    Wikipedia's plaintext extract marks a heading by wrapping it in equals
    signs, so the mark is what identifies one — reading the title out from
    between the marks, rather than stripping characters that could as easily
    have been part of it.
    """
    stripped = line.strip()
    if not (stripped.startswith(HEADING_MARK) and stripped.endswith(HEADING_MARK)):
        return None
    inner = stripped[len(HEADING_MARK) : -len(HEADING_MARK)]
    return inner.strip().lower()


def extract_section(text: str, section_title: str) -> str | None:
    """Extract a specific section from Wikipedia plaintext."""
    target = section_title.lower().strip()

    def under_heading() -> Iterator[str]:
        """The matching heading, then every line up to the next heading."""
        capturing = False
        for line in text.splitlines():
            heading = heading_title(line)
            if heading == target:
                capturing = True
                yield line
                continue
            if not capturing:
                continue
            if heading is not None:
                return
            yield line

    captured = list(under_heading())
    return "\n".join(captured) if captured else None


class TextOnly(HTMLParser):
    """Collect the text a fragment of markup renders, dropping its tags."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(text: str) -> str:
    """Remove HTML tags from a snippet.

    Parsed rather than pattern-matched: a search snippet is markup, and the
    ``<`` in a title the API quoted is not the start of a tag.
    """
    parser = TextOnly()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


WIKIPEDIA_TOOLS = [wiki_search, fetch_wikipedia]
