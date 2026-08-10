"""FRED (Federal Reserve Economic Data) API tools.

Access 500k+ US economic time series — GDP, unemployment, inflation,
interest rates, labor statistics, housing, and more.
"""

import logging
from typing import TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.config import current_settings
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"
MISSING_VALUE = "."
"""What FRED writes in an observation it has no reading for."""


class WireSeries(BaseModel):
    """One series as FRED describes it, by the names FRED sends."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = ""
    title: str = ""
    units: str = ""
    frequency: str = ""
    seasonal_adjustment: str = ""
    last_updated: str = ""

    def described(self, fallback_id: str = "") -> "FredSeriesInfo":
        """This series in the shape the tool answers with."""
        return FredSeriesInfo(
            series_id=self.id or fallback_id,
            title=self.title,
            units=self.units,
            frequency=self.frequency,
            seasonal_adjustment=self.seasonal_adjustment,
            last_updated=self.last_updated,
        )


class WireSeriesPage(BaseModel):
    """What a series search or lookup answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    seriess: list[WireSeries] = Field(default_factory=list)


class WireObservation(BaseModel):
    """One data point, whose value FRED writes as "." when it has none."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    date: str = ""
    value: str = MISSING_VALUE

    def recorded(self) -> bool:
        """Whether this point carries a reading rather than a gap."""
        return self.value != MISSING_VALUE


class WireObservations(BaseModel):
    """What an observations request answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    observations: list[WireObservation] = Field(default_factory=list)


class FredQuery(BaseModel):
    """What every FRED request carries, whatever it is asking for.

    A model rather than a mapping because the names are FRED's and closed:
    a request built here either spells one of them or does not compile.
    Unset fields are dropped so an absent bound is absent from the query.
    """

    model_config = ConfigDict(frozen=True)

    api_key: str
    file_type: str = "json"

    def sent(
        self,
    ) -> dict[
        str, str | int
    ]:  # lup: ignore[dict-str-payload] — an HTTP query string, which is what httpx takes
        """This query as the parameter mapping the client sends."""
        return self.model_dump(exclude_none=True)


class SeriesSearchQuery(FredQuery):
    """A search over FRED's series catalogue."""

    search_text: str
    limit: int
    order_by: str = "popularity"
    sort_order: str = "desc"


class SeriesLookupQuery(FredQuery):
    """A lookup of one series' metadata."""

    series_id: str


class ObservationsQuery(FredQuery):
    """A window of one series' observations."""

    series_id: str
    limit: int
    sort_order: str = "desc"
    observation_start: str | None = None
    observation_end: str | None = None


class FredObservation(TypedDict):
    date: str
    value: str


class FredSeriesInfo(TypedDict):
    series_id: str
    title: str
    units: str
    frequency: str
    seasonal_adjustment: str
    last_updated: str


class FredSearchInput(BaseModel):
    query: str = Field(description="Search query for FRED series")
    limit: int = Field(default=10, ge=1, le=50, description="Max results")


class FredSearchOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[FredSeriesInfo] = Field(description="Matching series")
    count: int = Field(description="Number of results returned")


class FredSeriesInput(BaseModel):
    series_id: str = Field(
        description="FRED series ID (e.g. 'UNRATE', 'CPIAUCSL', 'GDP', 'FEDFUNDS')"
    )
    start_date: str | None = Field(default=None, description="Start date (YYYY-MM-DD)")
    end_date: str | None = Field(default=None, description="End date (YYYY-MM-DD)")
    limit: int = Field(default=100, ge=1, le=1000, description="Max observations")


class FredSeriesOutput(BaseModel):
    series_id: str = Field(description="FRED series ID")
    info: FredSeriesInfo = Field(description="Series metadata")
    observations: list[FredObservation] = Field(description="Data points")
    count: int = Field(description="Number of observations returned")


def get_api_key() -> str:
    key = current_settings().fred_api_key
    if not key:
        raise ToolError("FRED_API_KEY not configured. Run `inkwell setup fred`.")
    return key


@lup_tool(
    "Search FRED for economic data series by keyword. Returns series IDs, "
    "titles, units, and frequency. Use this to discover the right series ID "
    "before calling fred_series to fetch actual data. Covers US economic "
    "indicators: GDP, unemployment (UNRATE), CPI inflation (CPIAUCSL), "
    "federal funds rate (FEDFUNDS), housing starts, labor participation, "
    "and 500k+ other time series."
)
async def fred_search(params: FredSearchInput) -> FredSearchOutput:
    api_key = get_api_key()
    url = f"{FRED_BASE}/series/search"
    query_params = SeriesSearchQuery(
        api_key=api_key, search_text=params.query, limit=params.limit
    ).sent()

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, params=query_params)
        if resp.status_code >= 400:
            raise ToolError(f"FRED API error {resp.status_code}: {resp.text[:200]}")
        page = WireSeriesPage.model_validate(resp.json())

    results = [series.described() for series in page.seriess]

    return FredSearchOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Fetch observations from a FRED economic data series. Returns time "
    "series data (date + value pairs) for a specific series ID. Use "
    "fred_search first to find the right series ID. Common series: "
    "UNRATE (unemployment), CPIAUCSL (CPI), GDP, FEDFUNDS (fed funds rate), "
    "PAYEMS (nonfarm payrolls), HOUST (housing starts), "
    "UMCSENT (consumer sentiment)."
)
async def fred_series(params: FredSeriesInput) -> FredSeriesOutput:
    api_key = get_api_key()

    info_url = f"{FRED_BASE}/series"
    info_params = SeriesLookupQuery(api_key=api_key, series_id=params.series_id).sent()

    obs_url = f"{FRED_BASE}/series/observations"
    obs_params = ObservationsQuery(
        api_key=api_key,
        series_id=params.series_id,
        limit=params.limit,
        observation_start=params.start_date or None,
        observation_end=params.end_date or None,
    ).sent()

    async with httpx.AsyncClient(timeout=20.0) as client:
        info_resp = await client.get(info_url, params=info_params)
        if info_resp.status_code >= 400:
            raise ToolError(
                f"FRED series '{params.series_id}' not found. "
                "Use fred_search to find valid series IDs."
            )
        described = WireSeriesPage.model_validate(info_resp.json())

        obs_resp = await client.get(obs_url, params=obs_params)
        obs_resp.raise_for_status()
        readings = WireObservations.model_validate(obs_resp.json())

    if not described.seriess:
        raise ToolError(f"Series '{params.series_id}' not found.")

    info = described.seriess[0].described(params.series_id)

    observations = [
        FredObservation(date=obs.date, value=obs.value)
        for obs in reversed(readings.observations)
        if obs.recorded()
    ]

    return FredSeriesOutput(
        series_id=params.series_id,
        info=info,
        observations=observations,
        count=len(observations),
    )


FRED_TOOLS = [fred_search, fred_series]
