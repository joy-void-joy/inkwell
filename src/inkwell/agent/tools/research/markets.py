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
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, computed_field

from inkwell.agent.provenance import Acquisition
from lup.mcp import ToolError, lup_tool
from lup.types import JsonValue

logger = logging.getLogger(__name__)


class MarketResult(TypedDict):
    platform: str
    title: str
    url: str
    probability: float | None
    volume: float | None
    liquidity: float | None
    close_date: str | None
    acquisition: Acquisition
    """How this price was acquired — record_finding takes it as it stands and
    derives the venue from it. A market price is dated by when it was read, not
    by a publication date, so record_finding asks for that date."""


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


def safe_float(value: JsonValue) -> float | None:
    """One wire number, however the platform chose to spell it."""
    if value is None:
        return None
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return None


class PolymarketMarket(BaseModel):
    """One market inside a Polymarket event, by the names the API sends."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    question: str = ""
    volume: JsonValue = None
    liquidity_num: JsonValue = Field(default=None, alias="liquidityNum")
    end_date: str | None = Field(default=None, alias="endDate")
    outcome_prices: str = Field(default="", alias="outcomePrices")
    clob_token_ids: str = Field(default="", alias="clobTokenIds")

    def probability(self) -> float | None:
        """The first outcome's price, which Polymarket sends as JSON in a string."""
        if not self.outcome_prices:
            return None
        try:
            prices = json.loads(self.outcome_prices)
        except json.JSONDecodeError:
            return None
        return safe_float(prices[0]) if isinstance(prices, list) and prices else None

    def token_ids(self) -> list[str]:
        """The CLOB tokens, which arrive the same JSON-in-a-string way."""
        if not self.clob_token_ids:
            return []
        try:
            ids = json.loads(self.clob_token_ids)
        except json.JSONDecodeError:
            return []
        return [str(token) for token in ids] if isinstance(ids, list) else []


class PolymarketEvent(BaseModel):
    """One event, which groups the markets asking about it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str = ""
    slug: str = ""
    description: str = ""
    markets: list[PolymarketMarket] = Field(default_factory=list)


class ManifoldMarket(BaseModel):
    """One Manifold market, by the names that API sends."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    question: str = ""
    slug: str = ""
    creator_username: str = Field(default="", alias="creatorUsername")
    probability: JsonValue = None
    volume: JsonValue = None
    total_liquidity: JsonValue = Field(default=None, alias="totalLiquidity")


class PricePoint(BaseModel):
    """One price reading, at the second Polymarket recorded it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    t: int = 0
    p: float = 0.0


class PriceHistory(BaseModel):
    """What a prices-history request answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    history: list[PricePoint] = Field(default_factory=list)


POLYMARKET_EVENTS = TypeAdapter(list[PolymarketEvent])
MANIFOLD_MARKETS = TypeAdapter(list[ManifoldMarket])
"""Both APIs answer with a bare array, which is what these validate."""


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
        events = POLYMARKET_EVENTS.validate_python(resp.json())

    def event_url(event: PolymarketEvent) -> str:
        """Where a reader opens this market, empty when the event names no slug."""
        return f"https://polymarket.com/event/{event.slug}" if event.slug else ""

    return [
        MarketResult(
            platform="polymarket",
            title=event.title or event.markets[0].question,
            url=event_url(event),
            acquisition=Acquisition(path="prediction_market", url=event_url(event)),
            probability=event.markets[0].probability(),
            volume=safe_float(event.markets[0].volume),
            liquidity=safe_float(event.markets[0].liquidity_num),
            close_date=event.markets[0].end_date,
        )
        for event in events
        if event.markets
    ][:limit]


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
        markets = MANIFOLD_MARKETS.validate_python(resp.json())

    def market_url(market: ManifoldMarket) -> str:
        """Where a reader opens this market, empty when it names no slug."""
        if not market.slug:
            return ""
        return f"https://manifold.markets/{market.creator_username}/{market.slug}"

    return [
        MarketResult(
            platform="manifold",
            title=m.question,
            url=market_url(m),
            acquisition=Acquisition(path="prediction_market", url=market_url(m)),
            probability=safe_float(m.probability),
            volume=safe_float(m.volume),
            liquidity=safe_float(m.total_liquidity),
            close_date=None,
        )
        for m in markets
    ][:limit]


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

    @computed_field
    @property
    def acquisition(self) -> Acquisition:
        """How this price was acquired — copy it into record_finding as it stands."""
        return Acquisition(path="prediction_market", url=self.url)


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
            events = POLYMARKET_EVENTS.validate_python(resp.json())
    except httpx.HTTPError as e:
        raise ToolError(f"Failed to fetch Polymarket event: {e}") from e

    if not events:
        raise ToolError(f"No event found for slug '{params.slug}'")

    event = events[0]

    title = event.title
    description = event.description

    market = event.markets[0] if event.markets else None
    probability = 0.5 if market is None else (market.probability() or 0.5)
    tokens = market.token_ids() if market is not None else []
    token_id = tokens[0] if tokens else None

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
                series = PriceHistory.model_validate(resp.json())

            by_day = {
                datetime.fromtimestamp(point.t, tz=timezone.utc).strftime("%Y-%m-%d"): (
                    point.p
                )
                for point in series.history
            }

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
        description=description,
        history=history,
    )


MARKET_TOOLS = [polymarket_search, manifold_search, search_markets, polymarket_price]
