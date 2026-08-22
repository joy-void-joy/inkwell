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
from inkwell.corpus.archive import closest_capture
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
    via: str = ""
    """Where this came from, when that was not the document's own URL.

    Empty for everything the host served, which is the overwhelming majority and
    needs no provenance beyond ``url``. Set to an archived capture's URL where a
    gated document was read from a public archive instead, because that is weaker
    evidence — a copy taken at a stated time — and a reader deciding whether to
    cite it needs to be able to tell.
    """

    def payload(self) -> bytes:
        """The bytes whose hash is this document's content identity."""
        return self.data if self.kind == "pdf" else self.text.encode("utf-8")


class FetchRefused(Exception):
    """A document that was reached but is not usable as one.

    Separate from a transport error because it is not worth retrying the same
    way: an anti-bot interstitial or an oversized body will look the same next
    time, and the failure classifier reads the distinction.
    """


class FetchGated(FetchRefused):
    """A document the host withheld from *us* rather than one that is unusable.

    The distinction a second party can act on. An oversized body and a page with
    no article text in it are the same for every reader there is, so asking
    anybody else for them would waste a request. A gate — a 401, a 403, an
    anti-bot interstitial — is about this connection, and a public archive that
    already holds the page is not behind it.

    A subclass rather than a flag, so every existing handler still catches it and
    only the one place that can do something different has to know the difference.
    """


CLIENT_REFUSED_STATUSES = (401, 403)
"""Statuses that mean "not for you" rather than "not there".

The whole of what makes reaching for a browser worth a page load. Kept here
rather than read off the failure vocabulary because it answers a different
question about the same numbers: that one names what went wrong for a report,
this one decides whether a second attempt could possibly go differently.
"""


def worth_a_browser(error: httpx.HTTPStatusError) -> bool:
    """Whether a browser could plausibly get a different answer to this.

    A host discriminating on the client — its user agent, its TLS handshake,
    the cookies it carries — may well answer a browser, which is the case the
    escalation exists for. A document that is absent is absent for every
    client there is, so a 404 put through a browser spends a page load to
    arrive at the same 404 wrapped in a longer traceback.
    """
    return error.response.status_code in CLIENT_REFUSED_STATUSES


def refuse_challenge(url: str, html: str, reached_by: str) -> None:
    """Refuse an anti-bot interstitial, naming the connection that got it.

    A status code is what catches a refusal on the plain path, and a browser
    has none to offer: what a host turns a browser away with is a page, served
    with a 200 and a body that extracts perfectly well. Checking here as well
    is what stops "the browser reached it" from meaning "the browser reached
    the block page and we stored that as the document".

    ``reached_by`` is in the message because the two cases call for opposite
    responses. A plain client meeting a challenge has somewhere left to go; a
    rendered browser page meeting one has exhausted what this project can do,
    and an operator reading the failure needs to know which they are looking
    at without reading this file.
    """
    marker = challenge_page_marker(html)
    if marker is not None:
        raise FetchGated(
            f"{url} served an anti-bot page to {reached_by} (matched {marker!r})"
        )


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
        except httpx.HTTPStatusError as error:
            if not (self.declaration.needs_browser and worth_a_browser(error)):
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
        """Fetch one document from its host, or from an archive that holds it.

        The host is always asked first and its answer is always preferred: an
        archived capture is a copy taken at some past moment, and reaching for one
        where the live page was available would date the corpus for no reason.

        The archive is asked only where the host refused *us* — a gate on this
        connection — and only where the source declares that fallback. A document
        that is absent, oversized, or holds no article text is that way for every
        reader there is, so asking a second party would spend a request to learn
        the same thing.
        """
        try:
            return await self.fetch_live(url, declaration)
        except httpx.HTTPStatusError as error:
            if not (declaration.archive_fallback and worth_a_browser(error)):
                raise
            return await self.fetch_archived(url, declaration, str(error))
        except FetchGated as gated:
            if not declaration.archive_fallback:
                raise
            return await self.fetch_archived(url, declaration, str(gated))

    async def fetch_live(
        self, url: str, declaration: SourceDeclaration
    ) -> FetchedDocument:
        """Fetch one document from its own host, extracting HTML and leaving a PDF.

        Raises ``FetchGated`` where the host withheld it from this client and
        ``FetchRefused`` where what came back is unusable to anybody, and lets
        transport errors through for the caller to classify.
        """
        try:
            response = await http_get(self.client, url)
        except httpx.HTTPStatusError as error:
            if not (declaration.needs_browser and worth_a_browser(error)):
                raise
            return await self.fetch_refused(url)

        if len(response.content) > self.max_bytes:
            raise FetchRefused(
                f"{url} is {len(response.content) // BYTES_PER_MB} MB, over the limit"
            )

        if looks_like_pdf(response):
            return FetchedDocument(url=url, kind="pdf", data=response.content)

        challenged = challenge_page_marker(response.text)
        if challenged is not None:
            if not declaration.needs_browser:
                raise FetchGated(
                    f"{url} served an anti-bot page to a plain client "
                    f"(matched {challenged!r})"
                )
            # An interstitial is cleared by running the script it came with,
            # which is the one thing the browser's bare connection does not do.
            return await self.fetch_rendered(url)

        extracted = extract_page(response.text, url=url, output_format=MARKDOWN_FORMAT)
        floor = declaration.thin_chars or self.thin_chars
        thin = extracted is None or len(extracted.text) < floor
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

    async def fetch_archived(
        self, url: str, declaration: SourceDeclaration, refused: str
    ) -> FetchedDocument:
        """Read one gated document from the newest capture a public archive holds.

        The capture goes through the same reading as a live response — the size
        limit, the PDF check, the challenge check, the thin floor — because it is
        the same kind of evidence about the same page, and a capture of an
        interstitial is exactly as useless as a live one. What differs is only
        where it came from, which the result records.

        ``refused`` is what the host said, carried into the failure where the
        archive has nothing either: an operator reading "no capture" needs to know
        what the live attempt was turned away with to know which to chase.
        """
        capture = await closest_capture(self.client, url)
        if not capture.found():
            raise FetchRefused(
                f"{url} was refused ({refused}) and no archived capture is "
                f"available: {capture.detail}"
            )
        response = await http_get(self.client, capture.url)
        if len(response.content) > self.max_bytes:
            raise FetchRefused(
                f"the capture of {url} is "
                f"{len(response.content) // BYTES_PER_MB} MB, over the limit"
            )
        if looks_like_pdf(response):
            return FetchedDocument(
                url=url, kind="pdf", data=response.content, via=capture.url
            )
        refuse_challenge(url, response.text, "a public archive's capture")
        extracted = extract_page(response.text, url=url, output_format=MARKDOWN_FORMAT)
        floor = declaration.thin_chars or self.thin_chars
        if extracted is None or len(extracted.text) < floor:
            raise FetchRefused(
                f"the archived capture of {url} holds no article text over "
                f"{floor} characters"
            )
        logger.info("Read %s from an archived capture of %s", url, capture.timestamp)
        return FetchedDocument(
            url=url,
            kind="markdown",
            title=extracted.title,
            published=extracted.published,
            text=extracted.text,
            via=capture.url,
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
        refuse_challenge(url, html, "the browser's connection")
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
        refuse_challenge(url, html, "a rendered browser page")
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
