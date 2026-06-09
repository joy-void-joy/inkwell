"""Exa AI-powered semantic web search.

Exa provides high-quality semantic search with livecrawl for fresh content,
domain filtering, date filtering, and highlight extraction. Primary research
tool for the writing pipeline.
"""

import logging
from typing import TypedDict

import httpx
from pydantic import BaseModel, Field

import inkwell.agent.config as config_mod
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)


class ExaResult(TypedDict):
    title: str | None
    url: str | None
    snippet: str | None
    highlights: list[str] | None
    published_date: str | None
    score: float | None


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
    "range filtering, and livecrawl for fresh content. Returns titles, "
    "URLs, text snippets, and highlighted key passages. "
    "Use fetch_url to read the full text of interesting results."
)
async def exa_search(params: ExaSearchInput) -> ExaSearchOutput:
    api_key = config_mod.settings.exa_api_key
    if not api_key:
        raise ToolError("EXA_API_KEY not configured. Run `inkwell setup exa`.")

    url = "https://api.exa.ai/search"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": api_key,
    }
    payload: dict[str, object] = {
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
        payload["includeDomains"] = params.include_domains
    if params.exclude_domains:
        payload["excludeDomains"] = params.exclude_domains

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if 400 <= response.status_code < 500:
            raise ToolError(
                f"Exa API error {response.status_code}: {response.text[:200]}"
            )
        response.raise_for_status()
        data = response.json()

    results: list[ExaResult] = []
    for r in data.get("results", []):
        published_date = r.get("publishedDate")
        if isinstance(published_date, str):
            published_date = published_date.rstrip("Z")

        results.append(
            ExaResult(
                title=r.get("title"),
                url=r.get("url"),
                snippet=(r.get("text") or "")[:500] or None,
                highlights=[h[:500] for h in r.get("highlights", [])[:3]] or None,
                published_date=published_date,
                score=r.get("score"),
            )
        )

    return ExaSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


EXA_TOOLS = [exa_search]
