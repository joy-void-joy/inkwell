"""Exa AI-powered semantic web search.

Exa provides high-quality semantic search with livecrawl for fresh content,
domain filtering, date filtering, and highlight extraction. Primary research
tool for the writing pipeline.
"""

import json
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

INLINE_RESULTS = 5
"""How many complete search records fit safely in one tool response."""

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
    """The result's text: a preview where `full_text_path` holds the whole,
    and the whole itself where no content directory was configured to hold
    it. Never a cut with nothing pointing at the rest."""
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
    results: list[ExaResult] = Field(description="Results carried in this response")
    count: int = Field(description="Total results Exa returned")
    returned: int = Field(description="Results carried inline")
    results_path: str | None = Field(
        default=None,
        description="Pretty-printed full result set to Read when count exceeds returned",
    )


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
    "enough to verify a claim. When Exa returns more than five hits, the "
    "response carries five inline and results_path points to the complete, "
    "pretty-printed result set for Read."
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
            logger.warning(
                "No content directory configured; carrying the full text of %s "
                "inline instead of pointing at a saved copy",
                hit.url or params.query,
            )
            return None

    def readable(hit: WireResult, saved: str | None) -> str | None:
        """The text this result carries, which is never less than all of it.

        A preview where the whole is on disk and the result points at it; the
        whole itself where there was nowhere to put it. Cutting in that second
        case is what loses content outright — the caller is handed 500
        characters and no path, which reads exactly like a short page.
        """
        if saved is None and len(hit.text) > SNIPPET_LENGTH:
            return hit.text or None
        # lup: ignore[silent-truncation] — the preview beside the whole, not
        # instead of it: `saved` is where the text is, and the branch above is
        # what answers the case where it is nowhere
        return hit.text[:SNIPPET_LENGTH] or None

    def result(hit: WireResult) -> ExaResult:
        """One hit, with its text either pointed at or carried."""
        saved = stored(hit)
        return ExaResult(
            acquisition=hit.acquisition(),
            title=hit.title or None,
            url=hit.url or None,
            snippet=readable(hit, saved),
            highlights=list(hit.highlights) or None,
            published_date=hit.published_at(),
            score=hit.score,
            full_text_path=saved,
        )

    results = [result(hit) for hit in found.results]

    def stored_results() -> str | None:
        """Where every result can be read when the inline page would overflow."""
        if len(results) <= INLINE_RESULTS:
            return None
        serializable = [
            {**one, "acquisition": one["acquisition"].model_dump()} for one in results
        ]
        try:
            return save_content(
                "exa-search", params.query, json.dumps(serializable, indent=2)
            ).path
        except RuntimeError:
            logger.warning("No content directory configured for Exa result manifest")
            return None

    results_path = stored_results()
    inline = (
        [one for index, one in enumerate(results) if index < INLINE_RESULTS]
        if results_path is not None
        else results
    )

    return ExaSearchOutput(
        query=params.query,
        results=inline,
        count=len(results),
        returned=len(inline),
        results_path=results_path,
    )


EXA_TOOLS = [exa_search]
