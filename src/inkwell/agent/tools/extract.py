"""Conversation and content extraction tools.

Extracts source material from Claude conversations, URLs, and local files
into structured text for the writing pipeline.
"""

# claude: ignore

import logging
import re
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from inkwell.agent.config import settings
from lup.mcp import ToolError, lup_tool

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


def extract_share_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def github_blob_to_raw(url: str) -> str | None:
    """Rewrite a GitHub blob URL to raw.githubusercontent.com."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/blob/(.+)", url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
    return None


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
    """Read all claude.ai cookies from Chrome's cookie store."""
    try:
        from pycookiecheat import chrome_cookies  # pyright: ignore[reportMissingImports]
        result = chrome_cookies("https://claude.ai")
        cookies = result if isinstance(result, dict) else {}
        if cookies:
            logger.info("Read %d cookies from Chrome", len(cookies))
        return {str(k): str(v) for k, v in cookies.items()}
    except Exception:
        logger.debug("Could not read cookies from browser", exc_info=True)
    return {}


def format_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def load_org_uuid_from_browser() -> str | None:
    cookies = load_cookies_from_browser()
    org_uuid = cookies.get("lastActiveOrg", "")
    return org_uuid or None


def load_cookie_from_browser() -> str | None:
    """Build a Cookie header string from Chrome's cookie store."""
    cookies = load_cookies_from_browser()
    if not cookies:
        return None
    header = format_cookie_header(cookies)
    logger.info("Loaded session cookie from Chrome (%d chars)", len(header))
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
        org_uuid = load_org_uuid_from_browser()

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
    """Prompt the user to paste a fresh Claude cookie."""
    import asyncio
    import webbrowser

    from rich.console import Console

    console = Console()
    console.print()
    console.print(f"[yellow bold]{reason}[/]")
    console.print()
    console.print("  To get a fresh cookie:")
    console.print("  1. Open claude.ai in your browser (opening now...)")
    console.print("  2. Open DevTools [bold]F12[/] → [bold]Network[/] tab")
    console.print("  3. Reload the page, click any request to claude.ai")
    console.print('  4. In "Request Headers", copy the full [bold]Cookie:[/] value')
    console.print("     [dim](the entire string, not just one cookie)[/]")
    console.print()

    try:
        webbrowser.open("https://claude.ai")
    except OSError:
        pass

    loop = asyncio.get_event_loop()
    cookie = await loop.run_in_executor(
        None, lambda: input("Paste cookie value (or Enter to abort): ").strip()
    )
    if not cookie:
        raise ToolError("Cookie refresh aborted — cannot extract conversation")
    return cookie


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

        cookie = settings.claude_cookie or load_cookie_from_browser()
        if not cookie:
            cookie = await prompt_for_cookie(
                "Share link requires authentication (no CLAUDE_COOKIE configured)."
            )

        try:
            data, org_uuid = await fetch_with_cookie(
                client, share_id, cookie, settings.claude_org_uuid
            )
        except CookieExpiredError:
            browser_cookie = load_cookie_from_browser()
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


@lup_tool(
    "Extract a Claude.ai conversation from a share link. Converts the conversation "
    "into structured markdown with <user> and <claude> speaker tags. Use this as "
    "the first step when the author provides a Claude conversation as source "
    "material. Returns both structured messages and formatted markdown. "
    "Requires CLAUDE_COOKIE in .env.local for org-restricted share links."
)
async def extract_conversation(
    params: ExtractConversationInput,
) -> ExtractConversationOutput:
    share_id = extract_share_id(params.url)
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

    return ExtractConversationOutput(
        url=params.url,
        messages=messages,
        markdown="\n\n".join(md_parts),
        message_count=len(messages),
    )


class ExtractUrlInput(BaseModel):
    url: str = Field(description="URL of an article, blog post, or web page to extract")


class ExtractUrlOutput(BaseModel):
    url: str = Field(description="Source URL")
    title: str = Field(default="", description="Page title if available")
    content: str = Field(description="Extracted text as markdown")
    word_count: int = Field(description="Approximate word count")


class ExtractFileInput(BaseModel):
    path: str = Field(description="Local file path (markdown, text, or PDF)")


class ExtractFileOutput(BaseModel):
    path: str = Field(description="Source file path")
    content: str = Field(description="Extracted text content")
    word_count: int = Field(description="Approximate word count")


@lup_tool(
    "Extract article text from a URL. Use this to ingest reference "
    "articles, blog posts, or any web page as source material for "
    "the writing pipeline. Better than fetch_url for source extraction "
    "because it focuses on clean, readable article text. Use for --ref "
    "URLs and style corpus URLs."
)
async def extract_url(params: ExtractUrlInput) -> ExtractUrlOutput:
    fetch_url = github_blob_to_raw(params.url) or params.url

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": CLAUDE_HEADERS["User-Agent"]},
        ) as client:
            resp = await client.get(fetch_url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ToolError(f"Failed to fetch {params.url}: {e}") from e

    if fetch_url != params.url:
        text = resp.text
        title = fetch_url.rsplit("/", 1)[-1]
    else:
        import trafilatura

        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            include_links=True,
            output_format="txt",
        )

        title = ""
        title_json = trafilatura.extract(
            resp.text, output_format="json", include_links=False
        )
        if title_json:
            import json

            try:
                title = json.loads(title_json).get("title", "")
            except (json.JSONDecodeError, AttributeError):
                pass

    if not text:
        raise ToolError(f"Could not extract text from {params.url}")

    return ExtractUrlOutput(
        url=params.url,
        title=title,
        content=text[:50000],
        word_count=len(text.split()),
    )


@lup_tool(
    "Extract text from a local file. Supports markdown (.md), plain "
    "text (.txt), and PDF files. Use this to ingest local reference "
    "documents provided by the author via --ref file paths."
)
async def extract_file(params: ExtractFileInput) -> ExtractFileOutput:
    file_path = Path(params.path).expanduser().resolve()
    if not file_path.exists():
        raise ToolError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        raise ToolError(
            f"PDF file detected: {file_path}. Use the Read tool to read PDFs directly."
        )

    try:
        raw = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"Could not read {file_path} as text. Supported formats: .md, .txt, .html"
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
        content=content[:50000],
        word_count=len(content.split()),
    )


async def do_extract_source(source: str) -> str:
    """Extract text from a Claude share link, URL, or file path.

    Used by the pipeline. Returns raw markdown text.
    For richer structured output, use the individual MCP tools.
    """
    if "claude.ai/share/" in source:
        share_id = extract_share_id(source)
        data = await fetch_snapshot(share_id)
        chat_messages = data.get("chat_messages", [])
        if not isinstance(chat_messages, list):
            raise RuntimeError("Unexpected response format: no chat_messages array")
        parts: list[str] = []
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
            parts.append(f"<{speaker}>\n{text}\n</{speaker}>")
        if not parts:
            raise RuntimeError("No messages found in conversation")
        return "\n\n".join(parts)

    path = Path(source).expanduser()
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise RuntimeError(f"Could not read {path} as text") from e

        if path.suffix.lower() in (".html", ".htm") or content.lstrip()[:100].lower().startswith(("<!doctype", "<html")):
            share_match = re.search(r"claude\.ai/share/([\w-]+)", content)
            if share_match:
                try:
                    return await do_extract_source(f"https://claude.ai/share/{share_match.group(1)}")
                except (RuntimeError, ToolError, httpx.HTTPError):
                    logger.info("API fetch failed for saved share page, falling back to HTML extraction")

            import trafilatura

            text = trafilatura.extract(content, include_comments=False, include_tables=True)
            if not text:
                raise RuntimeError(f"Could not extract text from HTML file: {path}")
            return text[:50000]

        return content[:50000]

    if source.startswith(("http://", "https://")):
        fetch_url = github_blob_to_raw(source) or source

        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": CLAUDE_HEADERS["User-Agent"]},
            ) as client:
                resp = await client.get(fetch_url)
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to fetch {source}: {e}") from e

        if fetch_url != source:
            return resp.text[:50000]

        import trafilatura

        text = trafilatura.extract(resp.text) or ""
        if not text:
            raise RuntimeError(f"Could not extract text from {source}")
        return text[:50000]

    return source


EXTRACT_TOOLS = [extract_conversation, extract_url, extract_file]
