"""Exa AI-powered semantic web search.

Exa provides high-quality semantic search with livecrawl for fresh content,
domain filtering, date filtering, and highlight extraction. Primary research
tool for the writing pipeline.
"""

import logging
from typing import TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.config import current_settings
from inkwell.agent.provenance import Acquisition
from lup.workspace.content_safety import save_content
from lup.mcp import ToolError, lup_tool
from lup.types import JsonObject

logger = logging.getLogger(__name__)

SNIPPET_LENGTH = 500
"""How much of a result is returned inline before it is saved to disk instead."""

UTC_SUFFIX = "Z"
"""What Exa appends to a timestamp, which callers here read without."""


class WireResult(BaseModel):
    """One search hit, by the names Exa sends."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str = ""
    url: str = ""
    text: str = ""
    highlights: list[str] = Field(default_factory=list)
    score: float | None = None
    published_date: str = Field(default="", alias="publishedDate")

    def published_at(self) -> str | None:
        """When this was published, without the zone marker Exa appends."""
        if not self.published_date:
            return None
        return self.published_date.removesuffix(UTC_SUFFIX)

    def acquisition(self) -> Acquisition:
        """How this hit was acquired, carrying the date Exa reported for it.

        A semantic search reaches whatever is indexed, so the path settles
        nothing about the venue and the host decides — which is exactly why the
        record travels with the result instead of being recalled later.
        """
        return Acquisition(
            path="exa_search", url=self.url, published=self.published_at() or ""
        )


class WireSearchResponse(BaseModel):
    """What an Exa search answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    results: list[WireResult] = Field(default_factory=list)


class ExaResult(TypedDict):
    title: str | None
    url: str | None
    snippet: str | None
    highlights: list[str] | None
    published_date: str | None
    score: float | None
    full_text_path: str | None
    """Path to the full result text on disk — Read it for complete content."""
    acquisition: Acquisition
    """How this result was acquired, carrying the date Exa reported for it.
    record_finding takes it as it stands: a semantic search reaches anything, so
    the venue derived from it comes from the host rather than from the search."""


class ExaSearchInput(BaseModel):
    query: str = Field(description="Search query")
    num_results: int = Field(default=10, ge=1, le=30, description="Number of results")
    livecrawl: str = Field(
        default="fallback",
        description="Livecrawl mode: 'always' (fresh), 'fallback' (cache first), 'never'",
    )
    include_domains: list[str] = Field(
        default_factory=list,
        description="Only include results from these domains",
    )
    exclude_domains: list[str] = Field(
        default_factory=list,
        description="Exclude results from these domains",
    )
    published_after: str | None = Field(
        default=None, description="ISO date lower bound (YYYY-MM-DD)"
    )
    published_before: str | None = Field(
        default=None, description="ISO date upper bound (YYYY-MM-DD)"
    )


class ExaSearchOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[ExaResult] = Field(description="Search results")
    count: int = Field(description="Number of results returned")


@lup_tool(
    "Search the web using Exa's semantic search engine. Use this for "
    "finding high-quality sources on any topic — academic articles, blog "
    "posts, news, technical documentation. Better than generic web search "
    "for research because it understands meaning, not just keywords. "
    "Supports domain filtering (include/exclude specific sites), date "
    "range filtering, and livecrawl for fresh content. Each result has a "
    "title, URL, snippet, highlighted key passages, and full_text_path — "
    "the complete page text saved to disk. Read full_text_path before "
    "citing or fact-checking against a result; the snippet alone is not "
    "enough to verify a claim."
)
async def exa_search(params: ExaSearchInput) -> ExaSearchOutput:
    api_key = current_settings().exa_api_key
    if not api_key:
        raise ToolError("EXA_API_KEY not configured. Run `inkwell setup exa`.")

    url = "https://api.exa.ai/search"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": api_key,
    }
    payload: JsonObject = {
        "query": params.query,
        "type": "auto",
        "useAutoprompt": True,
        "numResults": params.num_results,
        "livecrawl": params.livecrawl,
        "contents": {
            "text": {"includeHtmlTags": False},
            "highlights": {
                "query": params.query,
                "numSentences": 4,
                "highlightsPerUrl": 3,
            },
        },
    }

    if params.published_before:
        payload["endPublishedDate"] = f"{params.published_before}T23:59:59.999Z"
    if params.published_after:
        payload["startPublishedDate"] = f"{params.published_after}T00:00:00.000Z"
    if params.include_domains:
        payload["includeDomains"] = list(params.include_domains)
    if params.exclude_domains:
        payload["excludeDomains"] = list(params.exclude_domains)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if 400 <= response.status_code < 500:
            raise ToolError(f"Exa API error {response.status_code}: {response.text}")
        response.raise_for_status()
        found = WireSearchResponse.model_validate(response.json())

    def stored(hit: WireResult) -> str | None:
        """Where this hit's full text was saved, when it was long enough."""
        if len(hit.text) <= SNIPPET_LENGTH:
            return None
        try:
            return save_content("exa", hit.url or params.query, hit.text).path
        except RuntimeError:
            logger.debug("No content directory configured; returning snippet only")
            return None

    results = [
        ExaResult(
            acquisition=hit.acquisition(),
            title=hit.title or None,
            url=hit.url or None,
            snippet=hit.text[:SNIPPET_LENGTH] or None,
            highlights=list(hit.highlights) or None,
            published_date=hit.published_at(),
            score=hit.score,
            full_text_path=stored(hit),
        )
        for hit in found.results
    ]

    return ExaSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


EXA_TOOLS = [exa_search]
