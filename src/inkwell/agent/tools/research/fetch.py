"""URL fetch with content extraction.

Fetches any URL and extracts clean text using trafilatura.
Handles HTML, PDF, plain text, and JSON responses.
"""

import hashlib
import logging
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
import trafilatura
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

from inkwell.agent.config import stage_model
from inkwell.agent.provenance import Acquisition
from lup.workspace.content_safety import SavedContent, save_content
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)


def content_type(response: httpx.Response) -> str:
    """The content type a response declares, empty when it declares none.

    A server sends whatever headers it likes, so a missing one is ordinary
    and every caller here treats absent and blank the same way.
    """
    headers = response.headers
    return headers["content-type"] if "content-type" in headers else ""


class ExtractedMetadata(BaseModel):
    """The fields this project reads out of trafilatura's JSON output."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str = ""
    date: str = ""


class ExtractedPage(BaseModel):
    """An HTML page reduced to its article text and title.

    The one shape every caller of the extraction path below receives, so that
    reading a page means the same thing whether a tool call asked for it or the
    corpus did.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    title: str = ""
    published: str = ""
    """The date the page states it was published, ISO-8601, empty where it
    states none — which is ordinary, and never guessed at from anything else."""


def extract_page(
    html: str, *, url: str = "", output_format: str = "txt"
) -> ExtractedPage | None:
    """Extract one HTML page's article text and title, or None if there is none.

    The single trafilatura path in this project: navigation, ads, and comments
    go, links and tables stay. ``output_format`` is the one thing callers differ
    on — a tool call wants plain text to hand a reader, the corpus wants
    Markdown to keep on disk — so it is a parameter rather than a second copy
    of the call.
    """
    text = trafilatura.extract(
        html,
        url=url or None,
        output_format=output_format,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        include_links=True,
    )
    if not text:
        return None
    metadata = trafilatura.extract(html, output_format="json", include_links=False)
    described = ExtractedMetadata()
    if metadata:
        try:
            described = ExtractedMetadata.model_validate_json(metadata)
        except ValidationError:
            logger.debug("Extraction metadata for %s was not readable", url or "page")
    return ExtractedPage(text=text, title=described.title, published=described.date)


DOWNLOADS_DIR = Path("tmp/downloads")
MAX_PDF_BYTES = 100 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


GITHUB_HOST = "github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
BLOB_SEGMENT = 3
"""Where ``blob`` sits in ``/owner/repo/blob/ref/path`` once the path is split."""


def github_blob_to_raw(url: str) -> str | None:
    """Rewrite a GitHub blob URL to raw.githubusercontent.com.

    Read as a path rather than matched as a string: the raw host serves the
    same segments with ``blob`` dropped, so removing that one segment is the
    whole rewrite and a query or fragment on the original cannot leak into it.
    """
    parsed = urlparse(url)
    if parsed.hostname != GITHUB_HOST:
        return None
    segments = PurePosixPath(parsed.path).parts
    if len(segments) <= BLOB_SEGMENT + 1 or segments[BLOB_SEGMENT] != "blob":
        return None
    kept = [*segments[1:BLOB_SEGMENT], *segments[BLOB_SEGMENT + 1 :]]
    return f"{RAW_GITHUB}/{'/'.join(kept)}"


CHALLENGE_MARKERS = (
    "making sure you're not a bot",
    "checking your browser before accessing",
    "just a moment...",
    "verify you are human",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "ddos-guard",
)


def challenge_page_marker(text: str) -> str | None:
    """Detect anti-bot interstitials served in place of page content.

    Challenge pages are short HTML shells; real articles that merely
    mention these phrases are far longer, so size-gate the check.
    """
    if len(text) > 30_000:
        return None
    sample = text[:6000].lower()
    for marker in CHALLENGE_MARKERS:
        if marker in sample:
            return marker
    return None


class FetchSourceInput(BaseModel):
    url: str = Field(description="URL to fetch and extract text from")


class FetchSourceOutput(BaseModel):
    url: str = Field(description="Fetched URL")
    format: str = Field(description="Content format: 'text', 'pdf', 'json'")
    title: str = Field(default="", description="Page title if available")
    content: SavedContent = Field(description="Extracted text saved to disk")
    pdf_path: str | None = Field(
        default=None, description="Path to downloaded PDF (PDF format only)"
    )

    @computed_field
    @property
    def acquisition(self) -> Acquisition:
        """How this document was acquired, for record_finding to copy verbatim.

        Computed from the fetch rather than stored, so it cannot disagree with
        what happened. A bare fetch reaches whatever the URL serves, so the venue
        derived from it comes from the host: this path asserts nothing about the
        document beyond where it was found. The fetch learns no publication date,
        so record_finding asks for one.
        """
        return Acquisition(path="url_fetch", url=self.url)


async def do_fetch_source(url: str) -> FetchSourceOutput:
    """Fetch a URL and extract text. Callable from both MCP tools and Python."""
    resolved_url = github_blob_to_raw(url) or url

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(resolved_url)
    except httpx.TimeoutException as e:
        raise ToolError(f"Timeout fetching {url}") from e
    except httpx.ConnectError as e:
        raise ToolError(f"Could not connect to {url}") from e
    except httpx.HTTPError as e:
        raise ToolError(f"HTTP error fetching {url}: {e}") from e

    if 400 <= resp.status_code < 500:
        raise ToolError(
            f"HTTP {resp.status_code} for {url}. Try exa_search for cached content."
        )
    if resp.status_code >= 500:
        raise ToolError(f"Server error {resp.status_code} for {url}. Try again later.")

    ct = content_type(resp)

    if "html" in ct:
        marker = challenge_page_marker(resp.text)
        if marker is not None:
            raise ToolError(
                f"{url} served an anti-bot challenge page (matched {marker!r}) "
                f"instead of content. The fetch was blocked — try a mirror "
                f"or exa_search for cached content."
            )

    if "application/pdf" in ct:
        if len(resp.content) > MAX_PDF_BYTES:
            raise ToolError(
                f"PDF too large ({len(resp.content) / 1024 / 1024:.0f} MB)."
            )
        target = DOWNLOADS_DIR / "pdf"
        target.mkdir(parents=True, exist_ok=True)
        slug = hashlib.sha256(url.encode()).hexdigest()[:12]
        pdf_path = target / f"{slug}.pdf"
        pdf_path.write_bytes(resp.content)
        pdf_note = f"PDF downloaded to {pdf_path.resolve()}. Use Read to read it."
        saved = save_content("fetch", url, pdf_note)
        return FetchSourceOutput(
            url=url,
            format="pdf",
            content=saved,
            pdf_path=str(pdf_path.resolve()),
        )

    if "text/plain" in ct or "application/json" in ct:
        text = resp.text
        saved = save_content("fetch", url, text)
        return FetchSourceOutput(
            url=url,
            format="json" if "json" in ct else "text",
            content=saved,
        )

    if resolved_url != url:
        text = resp.text
        title = PurePosixPath(urlparse(resolved_url).path).name
        saved = save_content("fetch", url, text)
        return FetchSourceOutput(
            url=url,
            format="text",
            title=title,
            content=saved,
        )

    extracted = extract_page(resp.text, url=url)
    if extracted is not None:
        saved = save_content("fetch", url, extracted.text)
        return FetchSourceOutput(
            url=url,
            format="text",
            title=extracted.title,
            content=saved,
        )

    raise ToolError(
        f"Could not extract text from {url}. Try exa_search for indexed content."
    )


@lup_tool(
    "Fetch a URL and extract its text content. Saves the extracted text "
    "to disk and returns a file path, word count, and preview. Works "
    "with web pages, PDFs, plain text, JSON, and GitHub file URLs. "
    "For web pages, extracts the article text stripping navigation and "
    "ads. Use Read to access the full text. Use this whenever you "
    "encounter a URL you need to read — in source material, author "
    "feedback, GDoc comments, or research results."
)
async def fetch_source(params: FetchSourceInput) -> FetchSourceOutput:
    return await do_fetch_source(params.url)


EXTRACT_THRESHOLD_WORDS = 1000

FOCUSED_EXTRACT_SYSTEM = """\
You extract the relevant parts of a web page given a focus question.

Rules:
- Return ONLY the parts that answer or relate to the focus question
- Preserve exact quotes, numbers, names, and specific claims
- Include enough surrounding context that the extract stands alone
- If the page has nothing relevant, say so in one sentence
- Target 200-400 words. Go longer only if precision demands it
- Do NOT add commentary, analysis, or answers — just extract the relevant source text"""


class FocusedExtract(BaseModel):
    extract: str = Field(
        description="The relevant passages and information, preserving key details and quotes"
    )


class FetchAndExtractInput(BaseModel):
    url: str = Field(description="URL to fetch")
    focus: str = Field(
        description=(
            "What you need from this page — a question, topic, or context "
            "about why you're reading it. The extraction will return only "
            "the parts relevant to this focus."
        )
    )


class FetchAndExtractOutput(BaseModel):
    url: str = Field(description="Fetched URL")
    title: str = Field(default="", description="Page title if available")
    extract: str = Field(
        description="Focused extract — the parts relevant to your focus question"
    )
    word_count: int = Field(default=0, description="Word count of the extract")
    was_truncated: bool = Field(
        default=False,
        description="True if content was long enough to require extraction",
    )

    @computed_field
    @property
    def acquisition(self) -> Acquisition:
        """How this document was acquired — copy it into record_finding as it is."""
        return Acquisition(path="url_fetch", url=self.url)


async def do_fetch_and_extract(url: str, focus: str) -> FetchAndExtractOutput:
    """Fetch a URL and extract only the parts relevant to focus."""
    from inkwell.agent.client import query

    source = await do_fetch_source(url)
    content_path = source.content.path

    if source.format == "pdf":
        full_text = Path(content_path).read_text(encoding="utf-8")
        return FetchAndExtractOutput(
            url=url,
            title=source.title,
            extract=full_text,
            word_count=source.content.word_count,
        )

    if source.content.word_count <= EXTRACT_THRESHOLD_WORDS:
        full_text = Path(content_path).read_text(encoding="utf-8")
        return FetchAndExtractOutput(
            url=url,
            title=source.title,
            extract=full_text,
            word_count=source.content.word_count,
        )

    result = await query(
        (f"Focus: {focus}\n\nRead the page content from: {content_path}"),
        model=stage_model("extract"),
        system_prompt=FOCUSED_EXTRACT_SYSTEM,
        output_type=FocusedExtract,
        tools=["Read"],
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
    )

    if result is None:
        logger.warning(
            "Focused extraction failed for %s (%d words), returning full text via file",
            url,
            source.content.word_count,
        )
        full_text = Path(content_path).read_text(encoding="utf-8")
        return FetchAndExtractOutput(
            url=url,
            title=source.title,
            extract=full_text,
            word_count=source.content.word_count,
            was_truncated=False,
        )

    return FetchAndExtractOutput(
        url=url,
        title=source.title,
        extract=result.extract,
        word_count=len(result.extract) // 5,
        was_truncated=True,
    )


@lup_tool(
    "Fetch a URL and extract only the parts relevant to a specific focus. "
    "Use this instead of fetch_source when you have a specific question about "
    "a linked page — e.g. a URL in author feedback, a reference in a comment, "
    "or a citation you need to verify. Returns a focused extract (200-400 words) "
    "rather than the full page. For full source ingestion, use fetch_source instead."
)
async def fetch_and_extract(params: FetchAndExtractInput) -> FetchAndExtractOutput:
    return await do_fetch_and_extract(params.url, params.focus)


FETCH_TOOLS = [fetch_source, fetch_and_extract]
