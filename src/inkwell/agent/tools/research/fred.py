"""FRED (Federal Reserve Economic Data) API tools.

Access 500k+ US economic time series — GDP, unemployment, inflation,
interest rates, labor statistics, housing, and more.
"""

import logging
from typing import TypedDict

import httpx
from pydantic import BaseModel, Field

from inkwell.agent.config import current_settings
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"


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
    query_params = {
        "search_text": params.query,
        "api_key": api_key,
        "file_type": "json",
        "limit": params.limit,
        "order_by": "popularity",
        "sort_order": "desc",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, params=query_params)
        if resp.status_code >= 400:
            raise ToolError(f"FRED API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

    results: list[FredSeriesInfo] = []
    for s in data.get("seriess", []):
        results.append(
            FredSeriesInfo(
                series_id=s.get("id", ""),
                title=s.get("title", ""),
                units=s.get("units", ""),
                frequency=s.get("frequency", ""),
                seasonal_adjustment=s.get("seasonal_adjustment", ""),
                last_updated=s.get("last_updated", ""),
            )
        )

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
    info_params = {
        "series_id": params.series_id,
        "api_key": api_key,
        "file_type": "json",
    }

    obs_url = f"{FRED_BASE}/series/observations"
    obs_params: dict[str, str | int] = {
        "series_id": params.series_id,
        "api_key": api_key,
        "file_type": "json",
        "limit": params.limit,
        "sort_order": "desc",
    }
    if params.start_date:
        obs_params["observation_start"] = params.start_date
    if params.end_date:
        obs_params["observation_end"] = params.end_date

    async with httpx.AsyncClient(timeout=20.0) as client:
        info_resp = await client.get(info_url, params=info_params)
        if info_resp.status_code >= 400:
            raise ToolError(
                f"FRED series '{params.series_id}' not found. "
                "Use fred_search to find valid series IDs."
            )
        info_data = info_resp.json()

        obs_resp = await client.get(obs_url, params=obs_params)
        obs_resp.raise_for_status()
        obs_data = obs_resp.json()

    series_list = info_data.get("seriess", [])
    if not series_list:
        raise ToolError(f"Series '{params.series_id}' not found.")
    s = series_list[0]

    info = FredSeriesInfo(
        series_id=s.get("id", params.series_id),
        title=s.get("title", ""),
        units=s.get("units", ""),
        frequency=s.get("frequency", ""),
        seasonal_adjustment=s.get("seasonal_adjustment", ""),
        last_updated=s.get("last_updated", ""),
    )

    observations: list[FredObservation] = []
    for obs in obs_data.get("observations", []):
        value = obs.get("value", ".")
        if value != ".":
            observations.append(FredObservation(date=obs.get("date", ""), value=value))

    observations.reverse()

    return FredSeriesOutput(
        series_id=params.series_id,
        info=info,
        observations=observations,
        count=len(observations),
    )


FRED_TOOLS = [fred_search, fred_series]
