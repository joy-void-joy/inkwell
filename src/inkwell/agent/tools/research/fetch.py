"""URL fetch with content extraction.

Fetches any URL and extracts clean text using trafilatura.
Handles HTML, PDF, plain text, and JSON responses.
"""

import hashlib
import logging
from pathlib import Path

import httpx
import trafilatura
from pydantic import BaseModel, Field

from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path("tmp/downloads")
MAX_PDF_BYTES = 100 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FetchUrlInput(BaseModel):
    url: str = Field(description="URL to fetch and extract text from")


class FetchUrlOutput(BaseModel):
    url: str = Field(description="Fetched URL")
    format: str = Field(description="Content format: 'text', 'pdf', 'json'")
    title: str = Field(default="", description="Page title if available")
    content: str = Field(description="Extracted text content")
    pdf_path: str | None = Field(
        default=None, description="Path to downloaded PDF (PDF format only)"
    )


@lup_tool(
    "Fetch a URL and extract its text content. Works with web pages, "
    "PDFs, plain text, and JSON. For web pages, uses trafilatura to "
    "extract the article text, stripping navigation and ads. Use this "
    "after finding interesting URLs via exa_search or search_arxiv to "
    "read the full content. Returns clean, readable text."
)
async def fetch_url(params: FetchUrlInput) -> FetchUrlOutput:
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(params.url)
    except httpx.TimeoutException as e:
        raise ToolError(f"Timeout fetching {params.url}") from e
    except httpx.ConnectError as e:
        raise ToolError(f"Could not connect to {params.url}") from e
    except httpx.HTTPError as e:
        raise ToolError(f"HTTP error fetching {params.url}: {e}") from e

    if 400 <= resp.status_code < 500:
        raise ToolError(
            f"HTTP {resp.status_code} for {params.url}. "
            "Try exa_search for cached content."
        )
    if resp.status_code >= 500:
        raise ToolError(
            f"Server error {resp.status_code} for {params.url}. Try again later."
        )

    ct = resp.headers.get("content-type", "")

    if "application/pdf" in ct:
        if len(resp.content) > MAX_PDF_BYTES:
            raise ToolError(
                f"PDF too large ({len(resp.content) / 1024 / 1024:.0f} MB)."
            )
        target = DOWNLOADS_DIR / "pdf"
        target.mkdir(parents=True, exist_ok=True)
        slug = hashlib.sha256(params.url.encode()).hexdigest()[:12]
        pdf_path = target / f"{slug}.pdf"
        pdf_path.write_bytes(resp.content)
        return FetchUrlOutput(
            url=params.url,
            format="pdf",
            content=f"PDF downloaded to {pdf_path.resolve()}. Use Read to read it.",
            pdf_path=str(pdf_path.resolve()),
        )

    if "text/plain" in ct or "application/json" in ct:
        return FetchUrlOutput(
            url=params.url,
            format="json" if "json" in ct else "text",
            content=resp.text[:15000],
        )

    extracted = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        include_links=True,
    )
    if extracted:
        title = trafilatura.extract(
            resp.text, output_format="json", include_links=False
        )
        title_str = ""
        if title:
            import json

            try:
                title_str = json.loads(title).get("title", "")
            except (json.JSONDecodeError, AttributeError):
                pass
        return FetchUrlOutput(
            url=params.url,
            format="text",
            title=title_str,
            content=extracted[:15000],
        )

    raise ToolError(
        f"Could not extract text from {params.url}. Try exa_search for indexed content."
    )


FETCH_TOOLS = [fetch_url]
