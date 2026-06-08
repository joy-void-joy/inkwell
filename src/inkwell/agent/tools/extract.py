"""Conversation and content extraction tools.

Extracts source material from Claude conversations, URLs, and local files
into structured text for the writing pipeline.
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false

import logging
import re
import shutil
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from inkwell.agent.config import active_profile, settings
from lup.mcp import ToolError, lup_tool

CHUNK_CHARS = 100_000

logger = logging.getLogger(__name__)

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
    messages: list[ConversationMessage] = Field(description="Extracted messages")
    markdown: str = Field(description="Full conversation as markdown with speaker tags")
    message_count: int = Field(description="Number of messages extracted")
    markdown_path: str | None = Field(
        default=None,
        description="Path to full markdown file when content was too large to return inline",
    )


def extract_share_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def extract_block_text(block: dict[str, object]) -> str | None:
    """Extract text from a single content block, handling nested structures."""
    block_type = block.get("type", "")
    match block_type:
        case "text":
            text = block.get("text", "")
            return str(text) if text else None
        case "document" | "content":
            for field in ("text", "source", "content"):
                val = block.get(field, "")
                if isinstance(val, str) and val:
                    return val
            return None
        case "tool_result":
            nested = block.get("content", [])
            if isinstance(nested, list):
                inner = []
                for sub in nested:
                    if isinstance(sub, dict):
                        text = extract_block_text(sub)
                        if text:
                            inner.append(text)
                return "\n\n".join(inner) if inner else None
            if isinstance(nested, str) and nested:
                return nested
            return None
        case "image":
            return None
        case _:
            logger.debug(
                "Unhandled content block type=%s keys=%s",
                block_type,
                list(block.keys()),
            )
            for field in ("text", "content", "source"):
                val = block.get(field, "")
                if isinstance(val, str) and val:
                    logger.debug("Recovered text from unknown block type=%s via field=%s", block_type, field)
                    return val
            return None


def extract_attachments(msg: dict[str, object]) -> list[str]:
    """Extract text from message attachments (uploaded files, pasted content)."""
    attachments = msg.get("attachments", [])
    if not isinstance(attachments, list):
        return []
    parts: list[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        text = att.get("extracted_content")
        if isinstance(text, str) and text.strip():
            name = att.get("file_name", "")
            if isinstance(name, str) and name:
                parts.append(f"[Attachment: {name}]\n{text}")
            else:
                parts.append(text)
    return parts


def message_text(msg: dict[str, object]) -> str:
    content = msg.get("content", [])
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = extract_block_text(block)
        if text:
            parts.append(text)
    parts.extend(extract_attachments(msg))
    return "\n\n".join(parts)


class CookieExpiredError(Exception):
    """Raised when the Claude session cookie is expired or invalid."""


def load_cookies_from_browser() -> dict[str, str]:
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
            cookies = pool.submit(lambda: asyncio.run(extract_cookies(profile))).result(timeout=30)
    else:
        cookies = asyncio.run(extract_cookies(profile))

    if cookies:
        logger.info("Read %d cookies from persistent browser context", len(cookies))
    return cookies


def format_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def load_org_uuid_from_cookies(cookies: dict[str, str]) -> str | None:
    org_uuid = cookies.get("lastActiveOrg", "")
    return org_uuid or None


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
    auth_headers: dict[str, str],
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
    auth_headers: dict[str, str],
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
    orgs: list[dict[str, str]] = org_resp.json()
    if not orgs:
        raise ToolError("No organizations found for this account")
    return [o["uuid"] for o in orgs if "uuid" in o]


async def fetch_with_cookie(
    client: httpx.AsyncClient,
    share_id: str,
    cookie: str,
    org_uuid: str | None,
) -> tuple[dict[str, object], str]:
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
            result: dict[str, object] = resp.json()
            return result, uuid
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
                    return result, fallback_uuid

    raise ToolError(
        f"Conversation not found in any of {len(candidates)} org(s)"
    )


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
        console.print("  [dim]This session is stored separately from your main browser.[/dim]")
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

    env_updates: dict[str, str] = {"CLAUDE_COOKIE": cookie}
    if org_uuid:
        env_updates["CLAUDE_ORG_UUID"] = org_uuid
        settings.claude_org_uuid = org_uuid
    settings.claude_cookie = cookie
    write_env_local(env_updates)
    logger.info("Saved credentials to .env.local")


async def fetch_snapshot(share_id: str) -> dict[str, object]:
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
            result: dict[str, object] = resp.json()
            return result

        cookie = settings.claude_cookie
        if not cookie:
            cookie = await extract_cookie_header_async(active_profile())
        if not cookie:
            cookie = await prompt_for_cookie(
                "Share link requires authentication (no session cookie available)."
            )

        try:
            data, org_uuid = await fetch_with_cookie(
                client, share_id, cookie, settings.claude_org_uuid
            )
        except CookieExpiredError:
            browser_cookie = await extract_cookie_header_async(active_profile())
            if browser_cookie and browser_cookie != cookie:
                try:
                    data, org_uuid = await fetch_with_cookie(
                        client, share_id, browser_cookie, None
                    )
                    persist_credentials(browser_cookie, org_uuid)
                    return data
                except CookieExpiredError:
                    pass
            cookie = await prompt_for_cookie("Claude session cookie expired.")
            data, org_uuid = await fetch_with_cookie(
                client, share_id, cookie, None
            )

        persist_credentials(cookie, org_uuid)
        return data


async def do_extract_conversation(url: str) -> ExtractConversationOutput:
    """Extract a Claude conversation into structured markdown. No spill logic."""
    share_id = extract_share_id(url)
    data = await fetch_snapshot(share_id)

    chat_messages = data.get("chat_messages", [])
    if not isinstance(chat_messages, list):
        raise ToolError("Unexpected response format: no chat_messages array")

    messages: list[ConversationMessage] = []
    md_parts: list[str] = []

    for msg in chat_messages:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender", "")
        if not isinstance(sender, str):
            continue
        text = message_text(msg)
        if not text.strip():
            continue

        speaker = "user" if sender == "human" else "claude"
        messages.append(ConversationMessage(speaker=speaker, text=text))
        md_parts.append(f"<{speaker}>\n{text}\n</{speaker}>")

    if not messages:
        raise ToolError("No messages found in conversation")

    markdown = "\n\n".join(md_parts)
    return ExtractConversationOutput(
        url=url,
        messages=messages,
        markdown=markdown,
        message_count=len(messages),
    )


@lup_tool(
    "Extract a Claude.ai conversation from a share link. Converts the conversation "
    "into structured markdown with <user> and <claude> speaker tags. Use this as "
    "the first step when the author provides a Claude conversation as source "
    "material. Returns both structured messages and formatted markdown. "
    "For long conversations (>~4000 words), writes markdown to a file and returns "
    "the path — use Read to access the full text. "
    "Requires CLAUDE_COOKIE in .env.local for org-restricted share links."
)
async def extract_conversation(
    params: ExtractConversationInput,
) -> ExtractConversationOutput:
    from inkwell.agent.tools.content_spill import (
        chunked_spill_instruction,
        should_spill,
        spill_chunked,
    )

    result = await do_extract_conversation(params.url)
    if should_spill(result.markdown):
        envelope = spill_chunked("conversation", params.url, result.markdown)
        result = result.model_copy(update={
            "markdown_path": envelope.path,
            "markdown": chunked_spill_instruction(envelope),
        })
    return ExtractConversationOutput(
        url=params.url,
        messages=result.messages,
        markdown=result.markdown,
        message_count=result.message_count,
        markdown_path=result.markdown_path,
    )


class ExtractFileInput(BaseModel):
    path: str = Field(description="Local file path (markdown, text, or PDF)")


class ExtractFileOutput(BaseModel):
    path: str = Field(description="Source file path")
    content: str = Field(description="Extracted text content")
    word_count: int = Field(description="Approximate word count")
    content_path: str | None = Field(
        default=None,
        description="Path to full content file when content was too large to return inline",
    )


async def do_extract_file(file_path_str: str) -> ExtractFileOutput:
    """Extract text from a local file. No spill logic."""
    file_path = Path(file_path_str).expanduser().resolve()
    if not file_path.exists():
        raise ToolError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return ExtractFileOutput(
            path=str(file_path),
            content=f"[PDF file: {file_path.name}. Use the Read tool with "
            f'file_path="{file_path}" and pages="1-20" to read content. '
            f"Adjust page ranges to cover the full document.]",
            word_count=0,
        )

    try:
        raw = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"Could not read {file_path} as text. Supported formats: .md, .txt, .html, .pdf"
        )

    if suffix in (".html", ".htm") or raw.lstrip()[:100].lower().startswith(("<!doctype", "<html")):
        import trafilatura

        content = trafilatura.extract(raw, include_comments=False, include_tables=True) or ""
        if not content:
            raise ToolError(f"Could not extract text from HTML file: {file_path}")
    else:
        content = raw

    return ExtractFileOutput(
        path=str(file_path),
        content=content,
        word_count=len(content.split()),
    )


@lup_tool(
    "Extract text from a local file. Supports markdown (.md), plain "
    "text (.txt), HTML, and PDF files. For PDFs, returns the file path "
    "— use the Read tool to read PDF content directly (it supports "
    "page ranges). For large files (>~4000 words), writes content to a "
    "spill file and returns the path — use Read to access the full text. "
    "Use this to ingest local reference documents "
    "provided by the author via --ref file paths."
)
async def extract_file(params: ExtractFileInput) -> ExtractFileOutput:
    from inkwell.agent.tools.content_spill import (
        chunked_spill_instruction,
        should_spill,
        spill_chunked,
    )

    result = await do_extract_file(params.path)
    if should_spill(result.content):
        envelope = spill_chunked("file", params.path, result.content)
        result = result.model_copy(update={
            "content_path": envelope.path,
            "content": chunked_spill_instruction(envelope),
        })
    return result


def write_source_chunks(text: str, output_dir: Path, prefix: str = "source") -> list[Path]:
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

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2
        if current_len + para_len > CHUNK_CHARS and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    paths: list[Path] = []
    for i, chunk in enumerate(chunks, 1):
        path = output_dir / f"{prefix}_part{i}.md"
        path.write_text(chunk, encoding="utf-8")
        paths.append(path)

    return paths


def save_source_to_files(
    text: str,
    output_dir: Path,
    prefix: str = "source",
    pdf_source: Path | None = None,
) -> list[Path]:
    """Save extracted source to files for agent consumption.

    For PDF sources, copies the PDF to the output dir (agents read
    PDFs directly via the Read tool). For text, writes to chunked
    files if the text is large.
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
    """
    result = await do_extract_gdoc(url)

    source_tabs = [t for t in result.tabs if t.title == "Source"]
    if source_tabs:
        content = source_tabs[0].content
    else:
        content = "\n\n".join(
            t.content for t in result.tabs
            if t.title not in PIPELINE_TAB_NAMES
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
    content: str = Field(description="Post body as markdown")
    score: int = Field(description="Post karma score")
    comment_count: int = Field(description="Number of comments")
    posted_date: str = Field(description="Publication date (ISO format)")
    word_count: int = Field(description="Word count of post body")
    content_path: str | None = Field(
        default=None,
        description="Path to full content file when content was too large to return inline",
    )


def parse_lesswrong_slug(url: str) -> str:
    parts = url.rstrip("/").split("/")
    try:
        posts_idx = parts.index("posts")
    except ValueError:
        raise ToolError(f"Not a valid LessWrong URL (no /posts/ segment): {url}")
    if posts_idx + 2 >= len(parts):
        raise ToolError(f"Not a valid LessWrong URL (missing slug): {url}")
    return parts[posts_idx + 2]


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

    data = resp.json()
    post = data.get("data", {}).get("post", {}).get("result")
    if not post:
        raise ToolError(f"Post not found for slug '{parse_lesswrong_slug(url)}'")

    html_body: str = post.get("htmlBody", "")
    content = markdownify.markdownify(html_body, strip=["img"]) if html_body else ""

    user = post.get("user") or {}
    return ExtractLessWrongOutput(
        url=url,
        title=post.get("title", ""),
        author=user.get("displayName", "Unknown"),
        content=content,
        score=post.get("baseScore", 0),
        comment_count=post.get("commentCount", 0),
        posted_date=post.get("postedAt", ""),
        word_count=len(content.split()),
    )


@lup_tool(
    "Extract a LessWrong post with metadata via the GraphQL API. Returns "
    "the post body as markdown plus author, karma score, and comment count. "
    "For long posts (>~4000 words), writes content to a file and returns "
    "the path — use Read to access the full text. "
    "Use for style references, source material, or cross-referencing LW posts."
)
async def extract_lesswrong(
    params: ExtractLessWrongInput,
) -> ExtractLessWrongOutput:
    from inkwell.agent.tools.content_spill import (
        chunked_spill_instruction,
        should_spill,
        spill_chunked,
    )

    result = await do_extract_lesswrong(params.url)
    if should_spill(result.content):
        envelope = spill_chunked("lesswrong", params.url, result.content)
        result = result.model_copy(update={
            "content_path": envelope.path,
            "content": chunked_spill_instruction(envelope),
        })
    return result


class ExtractBatchInput(BaseModel):
    urls: list[str] = Field(description="URLs to extract text from (processed in parallel)")


class BatchResult(BaseModel):
    url: str
    title: str = ""
    content: str
    word_count: int
    content_path: str | None = None


class ExtractBatchOutput(BaseModel):
    results: list[BatchResult] = Field(description="Successfully extracted pages")
    failed: list[str] = Field(default_factory=list, description="URLs that failed")


@lup_tool(
    "Extract text from multiple URLs in parallel. Use when ingesting "
    "several reference pages at once — faster than calling fetch_source "
    "repeatedly. Returns extracted content for each URL and lists failures. "
    "For pages with >~4000 words, writes content to a file and returns "
    "the path — use Read to access the full text."
)
async def extract_webpage_batch(
    params: ExtractBatchInput,
) -> ExtractBatchOutput:
    import asyncio
    import json as json_mod

    import trafilatura

    from inkwell.agent.tools.content_spill import (
        chunked_spill_instruction,
        should_spill,
        spill_chunked,
    )

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
        title_json = trafilatura.extract(resp.text, output_format="json", include_links=False)
        if title_json:
            try:
                title = json_mod.loads(title_json).get("title", "")
            except (json_mod.JSONDecodeError, AttributeError):
                pass

        return BatchResult(url=url, title=title, content=text, word_count=len(text.split()))

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": CLAUDE_HEADERS["User-Agent"]},
    ) as client:
        tasks = [fetch_one(client, url) for url in params.urls]
        outcomes = await asyncio.gather(*tasks)

    results: list[BatchResult] = []
    failed: list[str] = []
    for url, outcome in zip(params.urls, outcomes):
        if outcome is None:
            failed.append(url)
        else:
            if should_spill(outcome.content):
                envelope = spill_chunked("batch", url, outcome.content)
                outcome = outcome.model_copy(update={
                    "content_path": envelope.path,
                    "content": chunked_spill_instruction(envelope),
                })
            results.append(outcome)

    return ExtractBatchOutput(results=results, failed=failed)


GDOC_URL_PATTERN = re.compile(
    r"https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)"
)

LINK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("claude_share", re.compile(r"https://claude\.ai/share/[a-zA-Z0-9_-]+")),
    ("url", re.compile(r"https?://[^\s<>\")\]]+(?<![.,;:!?\"'])")),
]


def parse_gdoc_id(url: str) -> str:
    match = GDOC_URL_PATTERN.search(url)
    if not match:
        raise ToolError(
            f"Not a Google Doc URL: {url}. "
            "Expected: https://docs.google.com/document/d/<doc_id>/..."
        )
    return match.group(1)


class DiscoveredLink(BaseModel):
    url: str = Field(description="Discovered URL")
    link_type: str = Field(
        description="Type: 'claude_share' for Claude conversations, 'url' for other links"
    )


def discover_links(text: str) -> list[DiscoveredLink]:
    """Scan text for embedded links (Claude shares, article URLs)."""
    seen: set[str] = set()
    links: list[DiscoveredLink] = []
    for link_type, pattern in LINK_PATTERNS:
        for match in pattern.finditer(text):
            url = match.group(0)
            if url in seen:
                continue
            if GDOC_URL_PATTERN.match(url):
                continue
            seen.add(url)
            links.append(DiscoveredLink(url=url, link_type=link_type))
    return links


class ExtractGdocInput(BaseModel):
    url: str = Field(
        description="Google Doc URL (https://docs.google.com/document/d/<id>/...)"
    )


class GdocTab(BaseModel):
    tab_id: str = Field(description="Tab ID")
    title: str = Field(description="Tab title")
    content: str = Field(description="Tab content as plain text")
    word_count: int = Field(description="Approximate word count")
    content_path: str | None = Field(
        default=None,
        description="Path to full content file when content was too large to return inline",
    )


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
    from inkwell.agent.tools.google_docs import execute_with_retry, services

    svc = services()
    drive = svc.drive_service()

    comments: list[GdocComment] = []
    page_token: str | None = None

    while True:
        kwargs: dict[str, object] = {
            "fileId": doc_id,
            "fields": "comments(id,content,author/displayName,quotedFileContent/value,replies/content,resolved),nextPageToken",
            "pageSize": 100,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        result = await execute_with_retry(drive.comments().list(**kwargs))
        items = result.get("comments", [])

        for item in items:
            if not isinstance(item, dict):
                continue
            if bool(item.get("resolved", False)):
                continue

            author_dict = item.get("author", {})
            author_name = ""
            if isinstance(author_dict, dict):
                author_name = str(author_dict.get("displayName", ""))

            anchor = ""
            quoted = item.get("quotedFileContent")
            if isinstance(quoted, dict):
                anchor = str(quoted.get("value", ""))

            replies_raw = item.get("replies", [])
            replies: list[str] = []
            if isinstance(replies_raw, list):
                for reply in replies_raw:
                    if isinstance(reply, dict):
                        rc = reply.get("content", "")
                        if isinstance(rc, str) and rc:
                            replies.append(rc)

            comments.append(
                GdocComment(
                    comment_id=str(item.get("id", "")),
                    author=author_name,
                    content=str(item.get("content", "")),
                    anchor_text=anchor,
                    replies=replies,
                    resolved=False,
                )
            )

        page_token_raw = result.get("nextPageToken")
        if isinstance(page_token_raw, str) and page_token_raw:
            page_token = page_token_raw
        else:
            break

    return comments


def format_comments_as_markdown(comments: list[GdocComment]) -> str:
    """Format extracted comments into markdown for inclusion in source text."""
    if not comments:
        return ""
    parts: list[str] = []
    for c in comments:
        header = f"**{c.author}**" if c.author else "Comment"
        if c.anchor_text:
            header += f' (on "{c.anchor_text}")'
        parts.append(f"- {header}: {c.content}")
        for reply in c.replies:
            parts.append(f"  - Reply: {reply}")
    return "## Comments\n\n" + "\n".join(parts)


async def do_extract_gdoc(url: str) -> ExtractGdocOutput:
    """Extract content, comments, and links from a Google Doc."""
    from inkwell.agent.tools.google_docs import (
        execute_with_retry,
        extract_tab_markdown,
        services,
    )

    doc_id = parse_gdoc_id(url)
    svc = services()
    docs = svc.docs_service()

    doc = await execute_with_retry(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )

    doc_title = str(doc.get("title", "Untitled"))
    tabs_raw = doc.get("tabs", [])
    if not isinstance(tabs_raw, list):
        raise ToolError("Could not read document tabs")

    tabs: list[GdocTab] = []
    all_text_parts: list[str] = []

    for tab in tabs_raw:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties", {})
        if not isinstance(props, dict):
            continue

        tab_id = str(props.get("tabId", ""))
        tab_title = str(props.get("title", ""))
        text = extract_tab_markdown(tab)
        all_text_parts.append(text)

        tabs.append(
            GdocTab(
                tab_id=tab_id,
                title=tab_title,
                content=text,
                word_count=len(text.split()),
            )
        )

    all_text = "\n".join(all_text_parts)
    discovered = discover_links(all_text)
    comments = await fetch_gdoc_comments(doc_id)

    return ExtractGdocOutput(
        doc_id=doc_id,
        title=doc_title,
        tabs=tabs,
        comments=comments,
        discovered_links=discovered,
        total_word_count=len(all_text.split()),
    )


@lup_tool(
    "Extract content from a Google Doc. Reads all tabs as markdown, comments, "
    "and discovers embedded links (Claude share links, article URLs). Use when "
    "the author provides a Google Doc as seed — reads its content, finds Claude "
    "conversations or reference URLs linked within, and returns structured source "
    "material. For large tabs (>~4000 words), writes content to a file and "
    "returns the path — use Read to access the full text. "
    "Discovered links can be processed with extract_conversation or "
    "fetch_source."
)
async def extract_gdoc(params: ExtractGdocInput) -> ExtractGdocOutput:
    from inkwell.agent.tools.content_spill import (
        chunked_spill_instruction,
        should_spill,
        spill_chunked,
    )

    result = await do_extract_gdoc(params.url)
    spilled_tabs: list[GdocTab] = []
    for tab in result.tabs:
        if should_spill(tab.content):
            envelope = spill_chunked("gdoc", f"{result.doc_id}_{tab.tab_id}", tab.content)
            tab = tab.model_copy(update={
                "content_path": envelope.path,
                "content": chunked_spill_instruction(envelope),
            })
        spilled_tabs.append(tab)
    return result.model_copy(update={"tabs": spilled_tabs})


EXTRACT_TOOLS = [
    extract_conversation, extract_file,
    extract_lesswrong, extract_webpage_batch, extract_gdoc,
]
