"""Reach one document, and say what came back without deciding what it means.

Two things arrive from a source and they are handled differently on purpose. An
HTML page goes through the *same* trafilatura extraction the fetch tool uses —
imported from ``research/fetch.py`` rather than copied — asking for Markdown so
that what lands on disk is what a reader greps. A PDF is not extracted at all:
its bytes come back untouched, because a PDF's notation and layout are the
document and an extraction of them is a plausible-looking wrong answer.

A source a plain fetcher cannot read says so in its declaration, and then the
fetch escalates to the profile's persistent browser context — the one
``browser_auth`` already runs for logins, not a second browser of its own. Two
things trigger it, because a source can be out of reach in two ways: a page
that should have text comes back thin, which is what a client-rendered shell
looks like from here, or the host refuses the fetcher outright. The refusal has
to be caught where the status is raised rather than judged from a body, since a
403 never produces one to judge — a source that answers a browser and refuses a
client would otherwise declare the browser path and never reach it.
"""

import logging
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from inkwell.agent.browser_auth import (
    BrowserRefused,
    fetched_through_browser,
    rendered_html,
)
from inkwell.agent.tools.research.fetch import (
    USER_AGENT,
    challenge_page_marker,
    content_type,
    extract_page,
)
from inkwell.corpus.discovery import PageReader
from inkwell.corpus.registry import SourceDeclaration

logger = logging.getLogger(__name__)

MARKDOWN_FORMAT = "markdown"
"""What the corpus asks the shared extraction path for: Markdown keeps the
headings, tables, and links that make a stored document navigable."""

PDF_CONTENT_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30.0
THIN_HTML_CHARS = 800
"""Below this, an HTML extraction is treated as a client-rendered shell rather
than a short article, and the browser path is worth trying."""

BYTES_PER_MB = 1024 * 1024


type FetchedKind = Literal["markdown", "pdf"]


class FetchedDocument(BaseModel):
    """One document as it arrived, before anything judges or stores it.

    Exactly one of ``text`` and ``data`` carries the document: Markdown for a
    page that was extracted, raw bytes for a PDF that was not.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    kind: FetchedKind
    title: str = ""
    published: str = ""
    text: str = ""
    data: bytes = b""
    rendered: bool = False

    def payload(self) -> bytes:
        """The bytes whose hash is this document's content identity."""
        return self.data if self.kind == "pdf" else self.text.encode("utf-8")


class FetchRefused(Exception):
    """A document that was reached but is not usable as one.

    Separate from a transport error because it is not worth retrying the same
    way: an anti-bot interstitial or an oversized body will look the same next
    time, and the failure classifier reads the distinction.
    """


async def http_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """One GET, raising for any status that is not a success."""
    response = await client.request("GET", url)
    response.raise_for_status()
    return response


class HttpPageReader(PageReader):
    """Reads pages over HTTP for discovery, on one shared client.

    The seam ``PageCache`` composes in a live run. Sitemaps and listing pages
    are ordinary GETs, and stay that way for a source that answers one.

    It carries its source's declaration for the one case that is not ordinary:
    a host that refuses a plain client refuses it at the feed as readily as at
    an article, and a reader with no declaration in hand would leave such a
    source enumerating nothing to fetch. Bytes, not a rendered DOM — a feed is
    XML that a parser is waiting for.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        declaration: SourceDeclaration,
        *,
        profile: str | None = None,
    ) -> None:
        self.client = client
        self.declaration = declaration
        self.profile = profile

    async def read(self, url: str) -> bytes:
        try:
            return (await http_get(self.client, url)).content
        except httpx.HTTPStatusError:
            if not self.declaration.needs_browser:
                raise
            return await fetched_through_browser(url, profile=self.profile)


def looks_like_pdf(response: httpx.Response) -> bool:
    """Whether a response is a PDF, by what it declares or how it begins.

    Some hosts serve PDFs as ``application/octet-stream``, and a PDF stored as
    if it were text would be unreadable, so the magic bytes get the final say.
    """
    declared = PDF_CONTENT_TYPE in content_type(response).lower()
    return declared or response.content[: len(PDF_MAGIC)] == PDF_MAGIC


class DocumentFetcher(BaseModel):
    """Fetches the documents of one corpus run.

    Parametrized by the declaration it is fetching for, because whether to
    reach for a browser is something the source says once rather than something
    guessed per page.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: httpx.AsyncClient
    profile: str | None = None
    thin_chars: int = THIN_HTML_CHARS
    max_bytes: int = MAX_DOCUMENT_BYTES

    async def fetch(self, url: str, declaration: SourceDeclaration) -> FetchedDocument:
        """Fetch one document, extracting HTML and leaving a PDF alone.

        Raises ``FetchRefused`` for something reached but unusable, and lets
        transport errors through for the caller to classify.
        """
        try:
            response = await http_get(self.client, url)
        except httpx.HTTPStatusError:
            if not declaration.needs_browser:
                raise
            return await self.fetch_refused(url)

        if len(response.content) > self.max_bytes:
            raise FetchRefused(
                f"{url} is {len(response.content) // BYTES_PER_MB} MB, over the limit"
            )

        if looks_like_pdf(response):
            return FetchedDocument(url=url, kind="pdf", data=response.content)

        marker = challenge_page_marker(response.text)
        if marker is not None:
            raise FetchRefused(f"{url} served an anti-bot page (matched {marker!r})")

        extracted = extract_page(response.text, url=url, output_format=MARKDOWN_FORMAT)
        thin = extracted is None or len(extracted.text) < self.thin_chars
        if thin and declaration.needs_browser:
            return await self.fetch_rendered(url)
        if extracted is None:
            raise FetchRefused(f"No article text could be extracted from {url}")
        return FetchedDocument(
            url=url,
            kind="markdown",
            title=extracted.title,
            published=extracted.published,
            text=extracted.text,
        )

    async def fetch_refused(self, url: str) -> FetchedDocument:
        """Fetch one document the plain client was turned away from.

        The browser's own connection rather than its DOM: what was wrong was
        the client, not the page, so rendering it would spend a page load to
        arrive at the same bytes.
        """
        try:
            body = await fetched_through_browser(url, profile=self.profile)
        except BrowserRefused as refusal:
            raise FetchRefused(str(refusal)) from refusal
        if body[: len(PDF_MAGIC)] == PDF_MAGIC:
            return FetchedDocument(url=url, kind="pdf", data=body, rendered=True)
        html = body.decode("utf-8", errors="replace")
        extracted = extract_page(html, url=url, output_format=MARKDOWN_FORMAT)
        if extracted is None:
            raise FetchRefused(f"No article text in what a browser reached at {url}")
        logger.info("Reached %s over the browser's connection after a refusal", url)
        return FetchedDocument(
            url=url,
            kind="markdown",
            title=extracted.title,
            published=extracted.published,
            text=extracted.text,
            rendered=True,
        )

    async def fetch_rendered(self, url: str) -> FetchedDocument:
        """Fetch one document through the profile's persistent browser context."""
        html = await rendered_html(url, profile=self.profile)
        extracted = extract_page(html, url=url, output_format=MARKDOWN_FORMAT)
        if extracted is None:
            raise FetchRefused(f"No article text after rendering {url}")
        logger.info("Rendered %s in a browser to reach its content", url)
        return FetchedDocument(
            url=url,
            kind="markdown",
            title=extracted.title,
            published=extracted.published,
            text=extracted.text,
            rendered=True,
        )


def corpus_client() -> httpx.AsyncClient:
    """The HTTP client a corpus run uses for both discovery and documents."""
    return httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
