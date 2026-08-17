"""Conversation and content extraction tools.

Extracts source material from Claude conversations, URLs, and local files
into structured text for the writing pipeline.
"""

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
from linkify_it import LinkifyIt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from inkwell.agent.config import active_profile, settings
from inkwell.pdf import reads_by_page
from lup.workspace.content_safety import SavedContent, save_content
from lup.mcp import ToolError, lup_tool
from lup.types import EnvVars, JsonValue, StringMap

CHUNK_CHARS = 100_000
PARAGRAPH_SEPARATOR = "\n\n"

LINKIFY = LinkifyIt()
"""Where a link starts and stops in prose, per markdown-it's own linkifier."""

logger = logging.getLogger(__name__)


class ExtractedMetadata(BaseModel):
    """The one field this project reads out of trafilatura's JSON output."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str = ""


CLAUDE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Origin": "https://claude.ai",
    "Connection": "keep-alive",
}


class ExtractConversationInput(BaseModel):
    url: str = Field(
        description="Claude.ai share URL (e.g., https://claude.ai/share/...)"
    )


class ConversationMessage(BaseModel):
    speaker: str = Field(description="'user' or 'claude'")
    text: str = Field(description="Message content")


class ExtractConversationOutput(BaseModel):
    url: str = Field(description="Original share URL")
    message_count: int = Field(description="Number of messages extracted")
    content: SavedContent = Field(
        description="Conversation saved to disk as markdown with speaker tags"
    )


def extract_share_id(url: str) -> str:
    """The conversation id a claude.ai share URL names."""
    share_id = claude_share_id(url)
    if share_id is None:
        raise ToolError(
            f"Not a Claude share URL: {url}. Expected: https://claude.ai/share/<id>"
        )
    return share_id


class ContentBlock(BaseModel):
    """One block of a message, in the shapes a shared conversation holds.

    Every text-bearing field is declared even though a given block type uses
    only one of them: which field carries the prose is what the type decides,
    and an unfamiliar type is read by trying them in turn rather than dropped.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str = ""
    text: str = ""
    source: str = ""
    content: "str | list[ContentBlock]" = ""

    def nested(self) -> list["ContentBlock"]:
        """The blocks inside this one, if it holds blocks rather than text."""
        return self.content if isinstance(self.content, list) else []

    def inline(self) -> str:
        """The text this block holds directly, if it holds text."""
        return self.content if isinstance(self.content, str) else ""

    def first_text(self) -> str | None:
        """The first text-bearing field this block actually filled."""
        for value in (self.text, self.source, self.inline()):
            if value:
                return value
        return None

    def prose(self) -> str | None:
        """The text this block contributes to the conversation transcript."""
        match self.type:
            case "text":
                return self.text or None
            case "document" | "content":
                return self.first_text()
            case "tool_result":
                inner = [
                    text
                    for block in self.nested()
                    if (text := block.prose()) is not None
                ]
                return "\n\n".join(inner) if inner else self.inline() or None
            case "image":
                return None
            case _:
                recovered = self.first_text()
                logger.debug(
                    "Unhandled content block type=%s recovered=%s",
                    self.type,
                    recovered is not None,
                )
                return recovered


class Attachment(BaseModel):
    """One file uploaded or pasted into a message."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    file_name: str = ""
    extracted_content: str = ""

    def prose(self) -> str | None:
        """This attachment's text, labelled with its name when it has one."""
        if not self.extracted_content.strip():
            return None
        if not self.file_name:
            return self.extracted_content
        return f"[Attachment: {self.file_name}]\n{self.extracted_content}"


class ShareMessage(BaseModel):
    """One message of a shared conversation.

    ``content`` is a list of blocks in every message the API has been seen to
    send, and a bare string in the older shape it still accepts; entries that
    are neither are dropped rather than failing the whole conversation, since
    one unreadable block should not cost the author the rest of the thread.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    sender: str = ""
    content: str | list[ContentBlock] = ""
    attachments: list[Attachment] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def drop_unreadable_blocks(cls, value: JsonValue) -> JsonValue:
        """Keep the blocks that are objects, and any bare string as it is."""
        if isinstance(value, list):
            return [block for block in value if isinstance(block, dict)]
        return value

    def blocks(self) -> list[ContentBlock]:
        """This message's blocks, or none when its content is a bare string."""
        return self.content if isinstance(self.content, list) else []

    def text(self) -> str:
        """Everything this message contributes, blocks then attachments."""
        if isinstance(self.content, str) and not self.attachments:
            return self.content
        return "\n\n".join(
            [
                *(
                    text
                    for block in self.blocks()
                    if (text := block.prose()) is not None
                ),
                *(
                    text
                    for attachment in self.attachments
                    if (text := attachment.prose()) is not None
                ),
            ]
        )


class ShareSnapshot(BaseModel):
    """A shared conversation, as far as extraction reads it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = ""
    chat_messages: list[ShareMessage] = Field(default_factory=list)


class FetchedSnapshot(BaseModel):
    """A snapshot, and which organization it turned out to live in."""

    snapshot: ShareSnapshot
    org_uuid: str


class Organization(BaseModel):
    """One account organization, of which only its id is read."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    uuid: str = ""


ORGANIZATIONS = TypeAdapter(list[Organization])
"""The organizations endpoint answers with a bare array."""


class CookieExpiredError(Exception):
    """Raised when the Claude session cookie is expired or invalid."""


def load_cookies_from_browser() -> StringMap:
    """Read all claude.ai cookies from the persistent browser context."""
    import asyncio

    from inkwell.agent.browser_auth import extract_cookies
    from inkwell.agent.config import active_profile

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    profile = active_profile()
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            cookies = pool.submit(lambda: asyncio.run(extract_cookies(profile))).result(
                timeout=30
            )
    else:
        cookies = asyncio.run(extract_cookies(profile))

    if cookies:
        logger.info("Read %d cookies from persistent browser context", len(cookies))
    return cookies


def format_cookie_header(cookies: StringMap) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def load_org_uuid_from_cookies(cookies: StringMap) -> str | None:
    from inkwell.agent.browser_auth import cookie_value

    return cookie_value(cookies, "lastActiveOrg") or None


def load_cookie_from_browser() -> str | None:
    """Build a Cookie header string from Chrome's cookie store."""
    cookies = load_cookies_from_browser()
    if not cookies:
        return None
    header = format_cookie_header(cookies)
    logger.info("Loaded session cookie from browser context (%d chars)", len(header))
    return header


async def fetch_snapshot_for_org(
    client: httpx.AsyncClient,
    share_id: str,
    org_uuid: str,
    auth_headers: StringMap,
) -> httpx.Response:
    """Try fetching a snapshot from a specific org. Returns the response."""
    org_url = (
        f"https://claude.ai/api/organizations/{org_uuid}/chat_snapshots/{share_id}"
        "?rendering_mode=messages&render_all_tools=true"
    )
    headers = {**auth_headers, "Referer": f"https://claude.ai/share/{share_id}"}
    return await client.get(org_url, headers=headers)


async def fetch_all_orgs(
    client: httpx.AsyncClient,
    auth_headers: StringMap,
) -> list[str]:
    """Fetch all org UUIDs for the authenticated account."""
    org_resp = await client.get(
        "https://claude.ai/api/organizations", headers=auth_headers
    )
    if org_resp.status_code in (401, 403):
        raise CookieExpiredError(
            f"Claude session expired (status {org_resp.status_code})"
        )
    if org_resp.status_code != 200:
        raise ToolError(
            f"Failed to fetch organizations (status {org_resp.status_code})"
        )
    orgs = ORGANIZATIONS.validate_python(org_resp.json())
    if not orgs:
        raise ToolError("No organizations found for this account")
    return [org.uuid for org in orgs if org.uuid]


async def fetch_with_cookie(
    client: httpx.AsyncClient,
    share_id: str,
    cookie: str,
    org_uuid: str | None,
) -> FetchedSnapshot:
    """Fetch snapshot using authenticated cookie. Returns (data, org_uuid).

    Tries the provided/cached org first; on 404, iterates all orgs.
    """
    auth_headers = {**CLAUDE_HEADERS, "Cookie": cookie}

    await client.get(
        f"https://claude.ai/share/{share_id}",
        headers={"User-Agent": CLAUDE_HEADERS["User-Agent"], "Cookie": cookie},
    )

    if not org_uuid:
        org_uuid = load_org_uuid_from_cookies(load_cookies_from_browser())

    candidates: list[str] = [org_uuid] if org_uuid else []

    if not candidates:
        candidates = await fetch_all_orgs(client, auth_headers)

    for uuid in candidates:
        resp = await fetch_snapshot_for_org(client, share_id, uuid, auth_headers)
        if resp.status_code in (401, 403):
            raise CookieExpiredError(
                f"Claude session expired (status {resp.status_code})"
            )
        if resp.status_code == 200:
            result = ShareSnapshot.model_validate(resp.json())
            return FetchedSnapshot(snapshot=result, org_uuid=uuid)
        if resp.status_code == 404 and len(candidates) == 1 and org_uuid:
            logger.info("Snapshot not found in org %s, trying all orgs", uuid[:8])
            all_orgs = await fetch_all_orgs(client, auth_headers)
            remaining = [o for o in all_orgs if o != uuid]
            for fallback_uuid in remaining:
                resp = await fetch_snapshot_for_org(
                    client, share_id, fallback_uuid, auth_headers
                )
                if resp.status_code in (401, 403):
                    raise CookieExpiredError(
                        f"Claude session expired (status {resp.status_code})"
                    )
                if resp.status_code == 200:
                    result = resp.json()
                    return FetchedSnapshot(snapshot=result, org_uuid=fallback_uuid)

    raise ToolError(f"Conversation not found in any of {len(candidates)} org(s)")


async def prompt_for_cookie(reason: str) -> str:
    """Try browser login first, fall back to manual cookie paste."""
    import asyncio

    from rich.console import Console

    from inkwell.agent.browser_auth import login_interactive
    from inkwell.agent.config import active_profile
    from inkwell.devtools.setup import has_graphical_display

    console = Console()
    console.print()
    console.print(f"[yellow bold]{reason}[/]")
    console.print()

    if has_graphical_display():
        console.print("  Opening a browser window — log in to claude.ai.")
        console.print(
            "  [dim]This session is stored separately from your main browser.[/dim]"
        )
        console.print()

        profile = active_profile()
        logged_in = await login_interactive(profile)
        if logged_in:
            cookie = await extract_cookie_header_async(profile)
            if cookie:
                console.print("[green]Login successful — session saved.[/]")
                return cookie

        console.print("[yellow]Browser login didn't produce valid cookies.[/]")
        console.print()

    console.print("  To provide a cookie manually:")
    console.print("  1. Open claude.ai in your browser, sign in")
    console.print("  2. Open DevTools [bold]F12[/] → [bold]Network[/] tab")
    console.print("  3. Reload the page, click any request to claude.ai")
    console.print('  4. In "Request Headers", copy the full [bold]Cookie:[/] value')
    console.print()

    loop = asyncio.get_event_loop()
    cookie = await loop.run_in_executor(
        None, lambda: input("Paste cookie value (or Enter to abort): ").strip()
    )
    if not cookie:
        raise ToolError("Cookie refresh aborted — cannot extract conversation")
    return cookie


async def extract_cookie_header_async(profile: str | None = None) -> str | None:
    """Async version of cookie header extraction from persistent browser context."""
    from inkwell.agent.browser_auth import extract_cookies

    cookies = await extract_cookies(profile)
    if not cookies:
        return None
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def persist_credentials(cookie: str, org_uuid: str | None) -> None:
    """Save cookie and org UUID to .env.local and update live settings."""
    from inkwell.devtools.setup import write_env_local

    env_updates: EnvVars = {"CLAUDE_COOKIE": cookie}
    if org_uuid:
        env_updates["CLAUDE_ORG_UUID"] = org_uuid
        settings.claude_org_uuid = org_uuid
    settings.claude_cookie = cookie
    write_env_local(env_updates)
    logger.info("Saved credentials to .env.local")


async def fetch_snapshot(share_id: str) -> ShareSnapshot:
    """Fetch conversation snapshot, trying public API first then org API.

    On auth failure, prompts the user for a fresh cookie, retries, and
    persists both the cookie and the org UUID for future runs.
    """
    public_url = (
        f"https://claude.ai/api/chat_snapshots/{share_id}"
        "?rendering_mode=messages&render_all_tools=true"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(public_url, headers=CLAUDE_HEADERS)
        if resp.status_code == 200:
            result = ShareSnapshot.model_validate(resp.json())
            return result

        cookie = settings.claude_cookie
        if not cookie:
            cookie = await extract_cookie_header_async(active_profile())
        if not cookie:
            cookie = await prompt_for_cookie(
                "Share link requires authentication (no session cookie available)."
            )

        try:
            fetched = await fetch_with_cookie(
                client, share_id, cookie, settings.claude_org_uuid
            )
        except CookieExpiredError:
            browser_cookie = await extract_cookie_header_async(active_profile())
            if browser_cookie and browser_cookie != cookie:
                try:
                    retried = await fetch_with_cookie(
                        client, share_id, browser_cookie, None
                    )
                    persist_credentials(browser_cookie, retried.org_uuid)
                    return retried.snapshot
                except CookieExpiredError:
                    logger.info("Browser cookie was expired too; prompting")
            cookie = await prompt_for_cookie("Claude session cookie expired.")
            fetched = await fetch_with_cookie(client, share_id, cookie, None)

        persist_credentials(cookie, fetched.org_uuid)
        return fetched.snapshot


async def do_extract_conversation(url: str) -> ExtractConversationOutput:
    """Extract a Claude conversation into structured markdown, saved to disk."""
    share_id = extract_share_id(url)
    data = await fetch_snapshot(share_id)

    spoken = [
        ConversationMessage(
            speaker="user" if msg.sender == "human" else "claude", text=text
        )
        for msg in data.chat_messages
        if (text := msg.text()).strip()
    ]

    if not spoken:
        raise ToolError("No messages found in conversation")

    messages = spoken
    markdown = "\n\n".join(f"<{m.speaker}>\n{m.text}\n</{m.speaker}>" for m in spoken)
    saved = save_content("conversation", share_id, markdown)

    return ExtractConversationOutput(
        url=url,
        message_count=len(messages),
        content=saved,
    )


@lup_tool(
    "Extract a Claude.ai conversation from a share link. Saves the conversation "
    "as structured markdown with <user> and <claude> speaker tags. Returns a "
    "file path, word count, and preview — use Read to access the full text. "
    "Use this as the first step when the author provides a Claude conversation "
    "as source material. "
    "Requires CLAUDE_COOKIE in .env.local for org-restricted share links."
)
async def extract_conversation(
    params: ExtractConversationInput,
) -> ExtractConversationOutput:
    return await do_extract_conversation(params.url)


class ExtractFileInput(BaseModel):
    path: str = Field(description="Local file path (markdown, text, or PDF)")


class ExtractFileOutput(BaseModel):
    source_path: str = Field(description="Original file path")
    content: SavedContent = Field(description="Extracted text saved to disk")


async def do_extract_file(file_path_str: str) -> ExtractFileOutput:
    """Extract text from a local file, saved to disk."""
    file_path = Path(file_path_str).expanduser().resolve()
    if not file_path.exists():
        raise ToolError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    if reads_by_page(file_path):
        pdf_note = (
            f"[PDF file: {file_path.name}. Use the Read tool with "
            f'file_path="{file_path}" and pages="1-20" to read content. '
            f"Adjust page ranges to cover the full document.]"
        )
        saved = save_content("file", file_path.stem, pdf_note)
        return ExtractFileOutput(source_path=str(file_path), content=saved)

    try:
        raw = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"Could not read {file_path} as text. Supported formats: .md, .txt, .html, .pdf"
        )

    if suffix in (".html", ".htm") or raw.lstrip()[:100].lower().startswith(
        ("<!doctype", "<html")
    ):
        import trafilatura

        content = (
            trafilatura.extract(raw, include_comments=False, include_tables=True) or ""
        )
        if not content:
            raise ToolError(f"Could not extract text from HTML file: {file_path}")
    else:
        content = raw

    saved = save_content("file", file_path.stem, content)
    return ExtractFileOutput(source_path=str(file_path), content=saved)


@lup_tool(
    "Extract text from a local file. Supports markdown (.md), plain "
    "text (.txt), HTML, and PDF files. Saves extracted content to disk "
    "and returns a file path, word count, and preview — use Read to "
    "access the full text. For PDFs, returns the original path for "
    "direct reading with page ranges. Use this to ingest local "
    "reference documents provided by the author via --ref file paths."
)
async def extract_file(params: ExtractFileInput) -> ExtractFileOutput:
    return await do_extract_file(params.path)


def write_source_chunks(
    text: str, output_dir: Path, prefix: str = "source"
) -> list[Path]:
    """Write text to one or more numbered chunk files.

    Returns the list of file paths written. For text shorter than
    CHUNK_CHARS, writes a single file. Splits on paragraph boundaries
    when possible.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if len(text) <= CHUNK_CHARS:
        path = output_dir / f"{prefix}.md"
        path.write_text(text, encoding="utf-8")
        return [path]

    paragraphs = text.split(PARAGRAPH_SEPARATOR)  # lup: ignore[string-split] — prose

    def chunked() -> Iterator[str]:
        """Successive runs of whole paragraphs that fit the chunk budget."""
        current = ""
        for para in paragraphs:
            addition = para if not current else f"{PARAGRAPH_SEPARATOR}{para}"
            if current and len(current) + len(addition) > CHUNK_CHARS:
                yield current
                current = para
                continue
            current += addition
        if current:
            yield current

    def written(index: int, chunk: str) -> Path:
        """One chunk on disk, numbered from one the way a reader counts."""
        path = output_dir / f"{prefix}_part{index}.md"
        path.write_text(chunk, encoding="utf-8")
        return path

    return [written(i, chunk) for i, chunk in enumerate(chunked(), 1)]


def save_source_to_files(
    text: str,
    output_dir: Path,
    prefix: str = "source",
    pdf_source: Path | None = None,
) -> list[Path]:
    """Save extracted source to files for agent consumption.

    For PDF sources, extracts text and writes to chunked markdown files.
    For text, writes to chunked files if the text is large.
    """
    if pdf_source is not None:
        dest = output_dir / pdf_source.name
        if not dest.exists():
            shutil.copy2(pdf_source, dest)
        return [dest]

    return write_source_chunks(text, output_dir, prefix)


PIPELINE_TAB_NAMES = {"Overview", "Plan", "Research", "Voice"}


async def extract_source_tab_only(url: str) -> str:
    """Extract only the Source tab from a pipeline Google Doc.

    When the source GDoc is the same as the output doc (re-running on an
    existing pipeline doc), reading all tabs would re-ingest the pipeline's
    own output (Plan, Research, section drafts). This reads only the
    "Source" tab, falling back to all non-pipeline tabs if "Source"
    doesn't exist.

    Returns the text content (reads saved files from disk).
    """
    result = await do_extract_gdoc(url)

    source_tabs = [t for t in result.tabs if t.title == "Source"]
    if source_tabs:
        content = Path(source_tabs[0].content.path).read_text(encoding="utf-8")
    else:
        content = "\n\n".join(
            Path(tab.content.path).read_text(encoding="utf-8")
            for tab in result.tabs
            if tab.title not in PIPELINE_TAB_NAMES
        )

    comments_md = format_comments_as_markdown(result.comments)
    if comments_md:
        content = f"{content}\n\n{comments_md}"

    return content


LESSWRONG_GRAPHQL_URL = "https://www.lesswrong.com/graphql"
LESSWRONG_QUERY = """
query GetPost($slug: String!) {
  post(input: {selector: {slug: $slug}}) {
    result {
      title
      htmlBody
      user { displayName }
      baseScore
      commentCount
      postedAt
    }
  }
}
"""


class ExtractLessWrongInput(BaseModel):
    url: str = Field(
        description="LessWrong post URL (e.g., https://www.lesswrong.com/posts/.../slug)"
    )


class ExtractLessWrongOutput(BaseModel):
    url: str = Field(description="Source URL")
    title: str = Field(description="Post title")
    author: str = Field(description="Author display name")
    score: int = Field(description="Post karma score")
    comment_count: int = Field(description="Number of comments")
    posted_date: str = Field(description="Publication date (ISO format)")
    content: SavedContent = Field(description="Post body saved to disk as markdown")


class LessWrongUser(BaseModel):
    """The post's author, as the GraphQL API names them."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    display_name: str = Field(default="Unknown", alias="displayName")


class LessWrongPost(BaseModel):
    """One post, by the names the GraphQL API sends."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    title: str = ""
    html_body: str = Field(default="", alias="htmlBody")
    base_score: int = Field(default=0, alias="baseScore")
    comment_count: int = Field(default=0, alias="commentCount")
    posted_at: str = Field(default="", alias="postedAt")
    user: LessWrongUser = LessWrongUser()


class LessWrongResult(BaseModel):
    """The single-post wrapper the query's result carries."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    result: LessWrongPost | None = None


class LessWrongData(BaseModel):
    """The data half of a GraphQL response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    post: LessWrongResult = LessWrongResult()


class LessWrongResponse(BaseModel):
    """What the LessWrong GraphQL endpoint answers with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    data: LessWrongData = LessWrongData()


POSTS_SEGMENT = "posts"
SLUG_OFFSET = 2
"""Where the slug sits after ``posts`` in ``/posts/<id>/<slug>``."""


def parse_lesswrong_slug(url: str) -> str:
    """The slug in a LessWrong post URL, read as a path."""
    segments = PurePosixPath(urlparse(url).path).parts
    if POSTS_SEGMENT not in segments:
        raise ToolError(f"Not a valid LessWrong URL (no /posts/ segment): {url}")
    slug_at = segments.index(POSTS_SEGMENT) + SLUG_OFFSET
    if slug_at >= len(segments):
        raise ToolError(f"Not a valid LessWrong URL (missing slug): {url}")
    return segments[slug_at]


async def do_extract_lesswrong(url: str) -> ExtractLessWrongOutput:
    """Fetch a LessWrong post via GraphQL. Returns structured output with post body only."""
    import markdownify

    slug = parse_lesswrong_slug(url)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                LESSWRONG_GRAPHQL_URL,
                json={"query": LESSWRONG_QUERY, "variables": {"slug": slug}},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise ToolError(f"Failed to fetch LessWrong post: {e}") from e

    if resp.status_code != 200:
        raise ToolError(f"LessWrong API returned HTTP {resp.status_code}")

    post = LessWrongResponse.model_validate(resp.json()).data.post.result
    if post is None:
        raise ToolError(f"Post not found for slug '{slug}'")

    content = (
        markdownify.markdownify(post.html_body, strip=["img"]) if post.html_body else ""
    )

    saved = save_content("lesswrong", post.title or url, content)
    return ExtractLessWrongOutput(
        url=url,
        title=post.title,
        author=post.user.display_name,
        score=post.base_score,
        comment_count=post.comment_count,
        posted_date=post.posted_at,
        content=saved,
    )


@lup_tool(
    "Extract a LessWrong post with metadata via the GraphQL API. Saves the "
    "post body as markdown to disk and returns a file path, word count, and "
    "preview plus author, karma score, and comment count. Use Read to access "
    "the full text. Use for style references, source material, or "
    "cross-referencing LW posts."
)
async def extract_lesswrong(
    params: ExtractLessWrongInput,
) -> ExtractLessWrongOutput:
    return await do_extract_lesswrong(params.url)


class ExtractBatchInput(BaseModel):
    urls: list[str] = Field(
        description="URLs to extract text from (processed in parallel)"
    )


class BatchResult(BaseModel):
    url: str
    title: str = ""
    content: SavedContent


class ExtractBatchOutput(BaseModel):
    results: list[BatchResult] = Field(description="Successfully extracted pages")
    failed: list[str] = Field(default_factory=list, description="URLs that failed")


@lup_tool(
    "Extract text from multiple URLs in parallel. Use when ingesting "
    "several reference pages at once — faster than calling fetch_source "
    "repeatedly. Saves each page to disk and returns file paths, word "
    "counts, and previews. Use Read to access the full text of any page."
)
async def extract_webpage_batch(
    params: ExtractBatchInput,
) -> ExtractBatchOutput:
    import asyncio

    import trafilatura

    if not params.urls:
        raise ToolError("No URLs provided")

    async def fetch_one(client: httpx.AsyncClient, url: str) -> BatchResult | None:
        try:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return None
        except httpx.HTTPError:
            return None

        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            include_links=True,
        )
        if not text:
            return None

        title = ""
        title_json = trafilatura.extract(
            resp.text, output_format="json", include_links=False
        )
        if title_json:
            try:
                title = ExtractedMetadata.model_validate_json(title_json).title
            except ValidationError:
                logger.debug("Extraction metadata for %s was not readable", url)

        saved = save_content("batch", url, text)
        return BatchResult(url=url, title=title, content=saved)

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": CLAUDE_HEADERS["User-Agent"]},
    ) as client:
        tasks = [fetch_one(client, url) for url in params.urls]
        outcomes = await asyncio.gather(*tasks)

    return ExtractBatchOutput(
        results=[outcome for outcome in outcomes if outcome is not None],
        failed=[url for url, outcome in zip(params.urls, outcomes) if outcome is None],
    )


CLAUDE_HOST = "claude.ai"
SHARE_SEGMENT = "share"


def claude_share_id(url: str) -> str | None:
    """The conversation id a claude.ai share URL names, if it names one."""
    parsed = urlparse(url)
    if parsed.hostname != CLAUDE_HOST:
        return None
    segments = PurePosixPath(parsed.path).parts
    if len(segments) < 3 or segments[1] != SHARE_SEGMENT:
        return None
    return segments[2]


def is_gdoc_url(url: str) -> bool:
    """Whether this URL names a Google Doc."""
    from inkwell.agent.tools.google_docs import doc_id_from_url

    return doc_id_from_url(url) is not None


def parse_gdoc_id(url: str) -> str:
    """The document id a Google Docs URL names, or an error saying it names none."""
    from inkwell.agent.tools.google_docs import doc_id_from_url

    doc_id = doc_id_from_url(url)
    if doc_id is None:
        raise ToolError(
            f"Not a Google Doc URL: {url}. "
            "Expected: https://docs.google.com/document/d/<doc_id>/..."
        )
    return doc_id


class DiscoveredLink(BaseModel):
    """One link found in an author's prose, and how it was written there.

    Where a link first appears and how often it is repeated are what separate
    a load-bearing source from one mentioned in passing, so a repeat is
    counted rather than discarded as a duplicate.
    """

    url: str = Field(description="Discovered URL")
    link_type: str = Field(
        description="Type: 'claude_share' for Claude conversations, 'url' for other links"
    )
    first_offset: int = Field(
        default=0, description="Character offset where the link first appears"
    )
    mentions: int = Field(default=1, description="How many times the link is written")

    def again(self) -> "DiscoveredLink":
        """The same link, having now been seen once more."""
        return self.model_copy(update={"mentions": self.mentions + 1})


def discover_links(text: str) -> list[DiscoveredLink]:
    """Scan text for embedded links (Claude shares, article URLs).

    Finding where a link starts and stops inside prose is markdown-it's
    linkifier's job — the same one that turns bare URLs into links when this
    project renders markdown, so a paste and its rendering agree about what
    was a link. Each one found is then read with urllib to say what it is.
    """
    from inkwell.agent.tools.google_docs import doc_id_from_url

    found: dict[str, DiscoveredLink] = {}  # lup: ignore[empty-collection] — a fold
    for match in LINKIFY.match(text) or []:
        if doc_id_from_url(match.url) is not None:
            continue
        seen = found[match.url] if match.url in found else None
        found[match.url] = (
            seen.again()
            if seen is not None
            else DiscoveredLink(
                url=match.url,
                link_type=(
                    "claude_share" if claude_share_id(match.url) is not None else "url"
                ),
                first_offset=match.index,
            )
        )
    return list(found.values())


class ExtractGdocInput(BaseModel):
    url: str = Field(
        description="Google Doc URL (https://docs.google.com/document/d/<id>/...)"
    )


class GdocTab(BaseModel):
    tab_id: str = Field(description="Tab ID")
    title: str = Field(description="Tab title")
    content: SavedContent = Field(description="Tab content saved to disk as markdown")


class GdocComment(BaseModel):
    comment_id: str = Field(description="Comment ID")
    author: str = Field(description="Comment author")
    content: str = Field(description="Comment text")
    anchor_text: str = Field(default="", description="Text the comment is anchored to")
    replies: list[str] = Field(default_factory=list, description="Reply texts")
    resolved: bool = Field(description="Whether the comment is resolved")


class ExtractGdocOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    title: str = Field(description="Document title")
    tabs: list[GdocTab] = Field(description="Content of each tab")
    comments: list[GdocComment] = Field(
        default_factory=list,
        description="Unresolved comments and replies from the document",
    )
    discovered_links: list[DiscoveredLink] = Field(
        description="Links found in the document (Claude shares, article URLs)"
    )
    total_word_count: int = Field(description="Total word count across all tabs")


async def fetch_gdoc_comments(doc_id: str) -> list[GdocComment]:
    """Read unresolved comments from a Google Doc via the Drive API."""
    from inkwell.agent.tools.google_docs import do_fetch_comments

    return [
        GdocComment(
            comment_id=entry.comment_id,
            author=entry.author,
            content=entry.content,
            anchor_text=entry.anchor_text,
            replies=entry.replies,
            resolved=False,
        )
        for entry in await do_fetch_comments(doc_id)
    ]


def format_comments_as_markdown(comments: list[GdocComment]) -> str:
    """Format extracted comments into markdown for inclusion in source text."""
    if not comments:
        return ""

    def lines() -> Iterator[str]:
        """Each comment as a bullet, with its replies nested beneath it."""
        for comment in comments:
            header = f"**{comment.author}**" if comment.author else "Comment"
            if comment.anchor_text:
                header += f' (on "{comment.anchor_text}")'
            yield f"- {header}: {comment.content}"
            yield from (f"  - Reply: {reply}" for reply in comment.replies)

    return "## Comments\n\n" + "\n".join(lines())


async def do_extract_gdoc(url: str) -> ExtractGdocOutput:
    """Extract content, comments, and links from a Google Doc."""
    from inkwell.agent.tools.google_docs import (
        extract_tab_markdown,
        fetch_document,
        services,
    )

    doc_id = parse_gdoc_id(url)
    svc = services()
    docs = svc.docs_service()

    doc = await fetch_document(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )

    doc_title = doc.title or "Untitled"
    rendered = [(tab.tab_properties, extract_tab_markdown(tab)) for tab in doc.walk()]
    tabs = [
        GdocTab(
            tab_id=props.tab_id,
            title=props.title,
            content=save_content("gdoc", f"{doc_title}-{props.title}", text),
        )
        for props, text in rendered
    ]
    total_words = sum(tab.content.word_count for tab in tabs)

    all_text = "\n".join(text for _, text in rendered)
    discovered = discover_links(all_text)
    comments = await fetch_gdoc_comments(doc_id)

    return ExtractGdocOutput(
        doc_id=doc_id,
        title=doc_title,
        tabs=tabs,
        comments=comments,
        discovered_links=discovered,
        total_word_count=total_words,
    )


@lup_tool(
    "Extract content from a Google Doc. Saves each tab as markdown to disk "
    "and returns file paths, word counts, and previews. Also reads comments "
    "and discovers embedded links (Claude share links, article URLs). "
    "Use Read to access the full text of any tab. Use when the author "
    "provides a Google Doc as seed. Discovered links can be processed "
    "with extract_conversation or fetch_source."
)
async def extract_gdoc(params: ExtractGdocInput) -> ExtractGdocOutput:
    return await do_extract_gdoc(params.url)


EXTRACT_TOOLS = [
    extract_conversation,
    extract_file,
    extract_lesswrong,
    extract_webpage_batch,
    extract_gdoc,
]
