"""arXiv paper search and retrieval.

Search for academic papers and fetch full text (HTML preferred, PDF fallback).
"""

import hashlib
import logging
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlparse

import arxiv
import httpx
import trafilatura
from pydantic import BaseModel, Field, computed_field

from lup.workspace.content_safety import SavedContent, save_content
from lup.mcp import ToolError, lup_tool

from inkwell.agent.provenance import Acquisition
from inkwell.agent.tools.research.fetch import content_type

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
    acquisition: Acquisition
    """How this paper was acquired, carrying arXiv's own publication date —
    record_finding takes it as it stands and derives the venue from it."""


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
    content: SavedContent | None = Field(
        default=None,
        description="Paper text saved to disk (HTML format). Use Read to access.",
    )
    pdf_path: str | None = Field(
        default=None, description="Path to downloaded PDF (PDF format only)"
    )

    @computed_field
    @property
    def acquisition(self) -> Acquisition:
        """How this paper was acquired — copy it into record_finding as it stands.

        arXiv serves preprints, so the venue follows from the path alone. Fetching
        full text reads no metadata, so the publication date is empty here and
        record_finding asks for it; search_arxiv reports it with each hit.
        """
        return Acquisition(path="arxiv", url=self.url)


ARXIV_HOST = "arxiv.org"
ARXIV_VIEWS = ("abs", "html", "pdf")
"""The path segments arXiv serves one paper under, whichever is linked."""

ID_SEPARATOR = 4
"""Where the dot sits in ``YYMM.NNNNN`` — the format is fixed-width."""

VERSION_MARK = "v"


def looks_like_arxiv_id(candidate: str) -> bool:
    """Whether this is arXiv's ``YYMM.NNNNN`` identifier, optionally versioned.

    Read by position because the format is fixed-width: four digits of year
    and month, a dot, four or five of sequence, and an optional version. A
    pattern over the whole string would also accept one embedded in prose.
    """
    marked = candidate.find(VERSION_MARK, ID_SEPARATOR)
    body = candidate if marked == -1 else candidate[:marked]
    if marked != -1 and not candidate[marked + 1 :].isdigit():
        return False
    if len(body) < ID_SEPARATOR + 5 or body[ID_SEPARATOR] != ".":
        return False
    return body[:ID_SEPARATOR].isdigit() and body[ID_SEPARATOR + 1 :].isdigit()


def parse_arxiv_id(raw: str) -> str | None:
    """The paper id in a bare identifier or any arXiv URL naming one."""
    candidate = raw.strip()
    parsed = urlparse(candidate)
    if parsed.hostname is not None:
        if parsed.hostname.removeprefix("www.") != ARXIV_HOST:
            return None
        segments = PurePosixPath(parsed.path).parts
        if len(segments) < 3 or segments[1] not in ARXIV_VIEWS:
            return None
        candidate = PurePosixPath(segments[2]).stem
    return candidate if looks_like_arxiv_id(candidate) else None


def result_to_paper(result: arxiv.Result) -> ArxivPaper:
    published = result.published.strftime("%Y-%m-%d")
    return ArxivPaper(
        acquisition=Acquisition(path="arxiv", url=result.entry_id, published=published),
        paper_id=result.entry_id,
        title=result.title,
        summary=result.summary if result.summary else None,
        authors=[str(a) for a in result.authors],
        published=published,
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
    results = [
        result_to_paper(result)
        for result in islice(client.results(search), params.max_results)
    ]

    return SearchArxivOutput(
        query=params.query,
        results=results,
        count=len(results),
    )


@lup_tool(
    "Fetch an arXiv paper's full text. Tries HTML first (fast, searchable); "
    "if unavailable, downloads the PDF and returns the file path for reading. "
    "HTML content is saved to disk — use Read with offset/limit to access. "
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
            if resp.status_code == 200 and "text/html" in content_type(resp):
                text = trafilatura.extract(resp.text) or ""
                if len(text) > 500:
                    saved = save_content("arxiv", paper_id, text)
                    return FetchArxivOutput(
                        paper_id=paper_id,
                        format="html",
                        url=html_url,
                        content=saved,
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
