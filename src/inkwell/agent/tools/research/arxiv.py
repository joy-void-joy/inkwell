"""arXiv paper search and retrieval.

Search for academic papers and fetch full text (HTML preferred, PDF fallback).
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import TypedDict

import arxiv
import httpx
import trafilatura
from pydantic import BaseModel, Field

from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path("tmp/downloads")
MAX_PDF_BYTES = 100 * 1024 * 1024


class ArxivPaper(TypedDict):
    paper_id: str
    title: str
    summary: str | None
    authors: list[str]
    published: str
    updated: str | None
    categories: list[str]
    primary_category: str
    pdf_url: str | None


class SearchArxivInput(BaseModel):
    query: str = Field(
        description="Search query (supports arXiv syntax: au:lastname, ti:word, cat:cs.AI)"
    )
    max_results: int = Field(
        default=10, ge=1, le=50, description="Maximum results to return"
    )


class SearchArxivOutput(BaseModel):
    query: str = Field(description="Original search query")
    results: list[ArxivPaper] = Field(description="Matching papers")
    count: int = Field(description="Number of results returned")


class FetchArxivInput(BaseModel):
    paper_id: str = Field(
        description=(
            "arXiv paper ID (e.g. '2301.12345' or '2301.12345v2'). "
            "Also accepts full URLs like 'https://arxiv.org/abs/2301.12345'."
        )
    )


class FetchArxivOutput(BaseModel):
    paper_id: str = Field(description="arXiv paper ID")
    format: str = Field(description="Content format: 'html' or 'pdf'")
    url: str = Field(description="Source URL")
    content: str | None = Field(
        default=None, description="Full paper text (HTML format only)"
    )
    pdf_path: str | None = Field(
        default=None, description="Path to downloaded PDF (PDF format only)"
    )


ARXIV_ID_PATTERN = re.compile(
    r"(?:https?://arxiv\.org/(?:abs|html|pdf)/)?(\d{4}\.\d{4,5}(?:v\d+)?)"
)


def parse_arxiv_id(raw: str) -> str | None:
    m = ARXIV_ID_PATTERN.search(raw)
    return m.group(1) if m else None


def result_to_paper(result: arxiv.Result) -> ArxivPaper:
    return ArxivPaper(
        paper_id=result.entry_id,
        title=result.title,
        summary=result.summary if result.summary else None,
        authors=[str(a) for a in result.authors],
        published=result.published.strftime("%Y-%m-%d"),
        updated=result.updated.strftime("%Y-%m-%d") if result.updated else None,
        categories=result.categories,
        primary_category=result.primary_category,
        pdf_url=result.pdf_url,
    )


@lup_tool(
    "Search arXiv for academic papers. Use this for questions about AI "
    "benchmarks, scientific discoveries, medical research, climate science, "
    "or any topic where peer-reviewed research provides evidence or base "
    "rates. Returns titles, full abstracts, authors, dates, and categories. "
    "Supports arXiv query syntax: au:lastname, ti:word, cat:cs.AI. "
    "Use fetch_arxiv to read the full text of interesting papers."
)
async def search_arxiv(params: SearchArxivInput) -> SearchArxivOutput:
    search = arxiv.Search(
        query=params.query,
        max_results=params.max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )

    client = arxiv.Client()
    results: list[ArxivPaper] = []
    for result in client.results(search):
        results.append(result_to_paper(result))
        if len(results) >= params.max_results:
            break

    return SearchArxivOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Fetch an arXiv paper's full text. Tries HTML first (fast, searchable); "
    "if unavailable, downloads the PDF and returns the file path for reading. "
    "Use search_arxiv first to find paper IDs, then fetch_arxiv to read them."
)
async def fetch_arxiv(params: FetchArxivInput) -> FetchArxivOutput:
    paper_id = parse_arxiv_id(params.paper_id)
    if not paper_id:
        raise ToolError(
            f"Could not parse arXiv ID from '{params.paper_id}'. "
            "Expected format: '2301.12345' or 'https://arxiv.org/abs/2301.12345'."
        )

    html_url = f"https://arxiv.org/html/{paper_id}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(html_url)
            if resp.status_code == 200 and "text/html" in resp.headers.get(
                "content-type", ""
            ):
                text = trafilatura.extract(resp.text) or ""
                if len(text) > 500:
                    return FetchArxivOutput(
                        paper_id=paper_id,
                        format="html",
                        url=html_url,
                        content=text[:30000],
                    )
        except httpx.HTTPError:
            logger.debug("HTML fetch failed for %s, trying PDF", paper_id)

        pdf_url = f"https://arxiv.org/pdf/{paper_id}"
        try:
            resp = await client.get(pdf_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError(f"Failed to fetch paper {paper_id}: {e}") from e

    if len(resp.content) > MAX_PDF_BYTES:
        raise ToolError(
            f"PDF too large ({len(resp.content) / 1024 / 1024:.0f} MB). "
            "Try the abstract instead."
        )

    target = DOWNLOADS_DIR / "arxiv"
    target.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha256(paper_id.encode()).hexdigest()[:12]
    pdf_path = target / f"{slug}.pdf"
    pdf_path.write_bytes(resp.content)

    return FetchArxivOutput(
        paper_id=paper_id,
        format="pdf",
        url=pdf_url,
        pdf_path=str(pdf_path.resolve()),
    )


ARXIV_TOOLS = [search_arxiv, fetch_arxiv]
