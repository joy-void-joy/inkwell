"""Prediction market tools — Polymarket and Manifold Markets.

Prediction market prices provide a unique epistemic signal: what do
informed bettors think about the probability of an event? Useful for
articles about forecasting, policy, technology, and current events.
"""

# claude: ignore

import json
import logging
from typing import TypedDict

import httpx
from pydantic import BaseModel, Field

from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)


class MarketResult(TypedDict):
    platform: str
    title: str
    url: str
    probability: float | None
    volume: float | None
    liquidity: float | None
    close_date: str | None


class PolymarketSearchInput(BaseModel):
    query: str = Field(description="Search query for Polymarket events")
    limit: int = Field(default=10, ge=1, le=20, description="Max results")


class PolymarketSearchOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[MarketResult] = Field(description="Matching markets")
    count: int = Field(description="Number of results")


class ManifoldSearchInput(BaseModel):
    query: str = Field(description="Search query for Manifold Markets")
    limit: int = Field(default=10, ge=1, le=20, description="Max results")


class ManifoldSearchOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[MarketResult] = Field(description="Matching markets")
    count: int = Field(description="Number of results")


class SearchMarketsInput(BaseModel):
    query: str = Field(
        description="Search query across all prediction market platforms"
    )
    limit: int = Field(default=10, ge=1, le=20, description="Max results per platform")


class SearchMarketsOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[MarketResult] = Field(description="Results from all platforms")
    count: int = Field(description="Total results")


def safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return None


def parse_polymarket_probability(market: dict[str, object]) -> float | None:
    prices = market.get("outcomePrices")
    if not prices or not isinstance(prices, str):
        return None
    try:
        parsed = json.loads(prices)
        if isinstance(parsed, list) and parsed:
            return safe_float(parsed[0])
    except (json.JSONDecodeError, IndexError):
        pass
    return None


async def query_polymarket(query: str, limit: int) -> list[MarketResult]:
    url = "https://gamma-api.polymarket.com/events"
    query_params = {
        "title_contains": query,
        "limit": limit,
        "active": "true",
        "order": "volume",
        "ascending": "false",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=query_params)
        resp.raise_for_status()
        data = resp.json()

    results: list[MarketResult] = []
    for event in data if isinstance(data, list) else []:
        markets = event.get("markets", [])
        for market in markets[:1]:
            slug = event.get("slug", "")
            results.append(
                MarketResult(
                    platform="polymarket",
                    title=event.get("title", market.get("question", "")),
                    url=f"https://polymarket.com/event/{slug}" if slug else "",
                    probability=parse_polymarket_probability(market),
                    volume=safe_float(market.get("volume")),
                    liquidity=safe_float(market.get("liquidityNum")),
                    close_date=market.get("endDate"),
                )
            )

    return results[:limit]


async def query_manifold(query: str, limit: int) -> list[MarketResult]:
    url = "https://api.manifold.markets/v0/search-markets"
    query_params = {
        "term": query,
        "limit": limit,
        "sort": "liquidity",
        "filter": "open",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=query_params)
        resp.raise_for_status()
        data = resp.json()

    results: list[MarketResult] = []
    for m in data if isinstance(data, list) else []:
        slug = m.get("slug", "")
        creator = m.get("creatorUsername", "")
        results.append(
            MarketResult(
                platform="manifold",
                title=m.get("question", ""),
                url=f"https://manifold.markets/{creator}/{slug}" if slug else "",
                probability=safe_float(m.get("probability")),
                volume=safe_float(m.get("volume")),
                liquidity=safe_float(m.get("totalLiquidity")),
                close_date=None,
            )
        )

    return results[:limit]


@lup_tool(
    "Search Polymarket for prediction markets. Returns market titles, "
    "current probabilities, trading volume, and close dates. Polymarket "
    "is the largest prediction market platform — strong signal for "
    "political events, economic forecasts, and tech milestones. "
    "Use search_markets for cross-platform search."
)
async def polymarket_search(
    params: PolymarketSearchInput,
) -> PolymarketSearchOutput:
    try:
        results = await query_polymarket(params.query, params.limit)
    except httpx.HTTPError as e:
        raise ToolError(f"Polymarket API error: {e}") from e

    return PolymarketSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Search Manifold Markets for prediction markets. Manifold covers a "
    "wider range of topics than Polymarket — AI timelines, scientific "
    "predictions, niche policy questions, community forecasts. Returns "
    "market titles, probabilities, and URLs. "
    "Use search_markets for cross-platform search."
)
async def manifold_search(params: ManifoldSearchInput) -> ManifoldSearchOutput:
    try:
        results = await query_manifold(params.query, params.limit)
    except httpx.HTTPError as e:
        raise ToolError(f"Manifold API error: {e}") from e

    return ManifoldSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Search across all prediction market platforms (Polymarket, Manifold) "
    "simultaneously. Returns combined results sorted by platform. Use this "
    "when you want the broadest view of market consensus on a topic — "
    "different platforms have different strengths and user bases. "
    "For platform-specific searches, use polymarket_search or manifold_search."
)
async def search_markets(params: SearchMarketsInput) -> SearchMarketsOutput:
    all_results: list[MarketResult] = []

    try:
        all_results.extend(await query_polymarket(params.query, params.limit))
    except httpx.HTTPError:
        logger.debug("Polymarket search failed for '%s'", params.query)

    try:
        all_results.extend(await query_manifold(params.query, params.limit))
    except httpx.HTTPError:
        logger.debug("Manifold search failed for '%s'", params.query)

    if not all_results:
        raise ToolError(
            f"No prediction markets found for '{params.query}' on any platform."
        )

    return SearchMarketsOutput(
        query=params.query,
        results=all_results,
        count=len(all_results),
    )


class PolymarketPriceInput(BaseModel):
    slug: str = Field(description="Polymarket event slug (from search results URL)")
    include_history: bool = Field(
        default=False, description="Include 7-day price history"
    )


class PriceHistoryPoint(TypedDict):
    date: str
    probability: float


class PolymarketPriceOutput(BaseModel):
    title: str = Field(description="Market title")
    probability: float = Field(description="Current YES probability (0-1)")
    url: str = Field(description="Market URL")
    description: str = Field(default="", description="Market description")
    history: list[PriceHistoryPoint] = Field(
        default_factory=list, description="Recent price history"
    )


@lup_tool(
    "Get detailed pricing for a specific Polymarket event by slug. Returns "
    "current probability, description, and optional 7-day price history. "
    "Use after search_markets to drill into a specific market's price trajectory."
)
async def polymarket_price(
    params: PolymarketPriceInput,
) -> PolymarketPriceOutput:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": params.slug},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise ToolError(f"Failed to fetch Polymarket event: {e}") from e

    events = data if isinstance(data, list) else [data]
    if not events:
        raise ToolError(f"No event found for slug '{params.slug}'")

    event = events[0]
    if not isinstance(event, dict):
        raise ToolError("Invalid event data from Polymarket")

    title = str(event.get("title", ""))
    description = str(event.get("description", ""))
    markets = event.get("markets", [])

    probability = 0.5
    token_id: str | None = None
    if isinstance(markets, list) and markets:
        market = markets[0]
        if isinstance(market, dict):
            probability = parse_polymarket_probability(market) or 0.5

            clob_ids = market.get("clobTokenIds")
            if isinstance(clob_ids, str):
                try:
                    parsed = json.loads(clob_ids)
                    if isinstance(parsed, list) and parsed:
                        token_id = str(parsed[0])
                except (json.JSONDecodeError, IndexError):
                    pass
            elif isinstance(clob_ids, list) and clob_ids:
                token_id = str(clob_ids[0])

    history: list[PriceHistoryPoint] = []
    if params.include_history and token_id:
        import time
        from datetime import datetime, timezone

        end_ts = int(time.time())
        start_ts = end_ts - 7 * 86400

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://clob.polymarket.com/prices-history",
                    params={"market": token_id, "startTs": start_ts, "endTs": end_ts},
                )
                resp.raise_for_status()
                hist_data = resp.json()

            by_day: dict[str, float] = {}
            for point in hist_data.get("history", []):
                if isinstance(point, dict):
                    ts = point.get("t", 0)
                    price = point.get("p", 0)
                    day = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                        "%Y-%m-%d"
                    )
                    by_day[day] = float(price)

            history = [
                PriceHistoryPoint(date=d, probability=p)
                for d, p in sorted(by_day.items())
            ]
        except httpx.HTTPError:
            logger.warning("Failed to fetch price history for %s", params.slug)

    return PolymarketPriceOutput(
        title=title,
        probability=probability,
        url=f"https://polymarket.com/event/{params.slug}",
        description=description[:500],
        history=history,
    )


MARKET_TOOLS = [polymarket_search, manifold_search, search_markets, polymarket_price]
