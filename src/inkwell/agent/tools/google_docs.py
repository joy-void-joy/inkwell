"""Google Docs MCP tools for live document collaboration.

The agent writes into a Google Doc that the author follows in real time.
Uses tabs for parallel section writing and comments for async communication.

Requires Google OAuth credentials configured via `inkwell setup`.
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false, reportMissingImports=false
# googleapiclient returns untyped Resource objects throughout.

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

from inkwell.agent.google_auth import ServiceFactory, get_service_factory
from inkwell.agent.markdown_to_docs import (
    clamp_ranges,
    clear_tab_request,
    markdown_to_requests,
    split_markdown_batch,
)
from lup.content_safety import SavedContent, save_content
from lup.mcp import ToolError, lup_tool

if TYPE_CHECKING:
    from inkwell.agent.session import WritingSessionState

logger = logging.getLogger(__name__)

SERVICES: ServiceFactory | None = None
SESSION_STATE_VAR: contextvars.ContextVar[WritingSessionState | None] = (
    contextvars.ContextVar("session_state", default=None)
)


def configure_session_state(state: WritingSessionState) -> None:
    SESSION_STATE_VAR.set(state)


def get_session_state() -> WritingSessionState | None:
    return SESSION_STATE_VAR.get()


def services() -> ServiceFactory:
    global SERVICES  # noqa: PLW0603
    if SERVICES is None:
        try:
            SERVICES = get_service_factory()
        except RuntimeError as e:
            raise ToolError(str(e)) from e
    return SERVICES


def require_doc_id() -> str:
    """Get doc_id from session state. Raises ToolError if no doc exists."""
    state = get_session_state()
    if state is None or not state.doc_id:
        raise ToolError("No Google Doc created yet. Use create_doc first.")
    return state.doc_id


RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


async def execute_with_retry(
    request: object,
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
) -> object:
    """Execute a Google API request with retry on transient errors."""
    for attempt in range(max_attempts):
        try:
            return request.execute()  # type: ignore[union-attr]
        except HttpError as exc:
            if exc.resp.status not in RETRYABLE_STATUS_CODES:
                raise
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2**attempt)
            logger.warning(
                "Google API %d, retrying in %.1fs (attempt %d/%d)",
                exc.resp.status,
                delay,
                attempt + 1,
                max_attempts,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable")


@asynccontextmanager
async def gdoc_nonfatal(operation: str) -> AsyncGenerator[None]:
    """Wrap GDoc calls so failures log a warning instead of crashing the pipeline.

    The Google Doc is a display surface — the pipeline snapshot holds the real
    data.  If a write/comment/tab-create fails, the author sees a stale Doc
    but the pipeline keeps running.
    """
    try:
        yield
    except (HttpError, OSError, TimeoutError, ToolError) as exc:
        logger.warning("GDoc %s failed (non-fatal): %s", operation, exc)


# ---------------------------------------------------------------------------
# Input / Output schemas
# ---------------------------------------------------------------------------


class CreateDocInput(BaseModel):
    title: str = Field(description="Document title")
    share_with: str | None = Field(
        default=None, description="Email address to share the doc with (editor access)"
    )


class CreateDocOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    url: str = Field(description="Google Doc URL")


class CreateTabInput(BaseModel):
    tab_name: str = Field(description="Name for the new tab")


class CreateTabOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="ID of the created tab")
    tab_name: str = Field(description="Name of the created tab")


class WriteTabInput(BaseModel):
    tab_id: str = Field(description="Tab ID to write to")
    content: str = Field(description="Markdown content to write (replaces tab content)")


class WriteTabOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="Tab that was written to")
    characters_written: int = Field(description="Number of characters written")


class ReadTabInput(BaseModel):
    tab_id: str | None = Field(
        default=None, description="Tab ID to read (None = first/default tab)"
    )


class ReadTabOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="Tab that was read")
    content: SavedContent = Field(description="Tab content saved to disk")


class InsertCommentInput(BaseModel):
    content: str = Field(description="Comment text (e.g., a question for the author)")
    anchor_text: str | None = Field(
        default=None,
        description="Text in the doc to anchor the comment to. If None, comment is unanchored.",
    )


class InsertCommentOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    comment_id: str = Field(description="ID of the created comment")
    content: str = Field(description="Comment text")


class ReadCommentsInput(BaseModel):
    include_resolved: bool = Field(
        default=False, description="Whether to include resolved comments"
    )


class CommentEntry(BaseModel):
    comment_id: str = Field(description="Comment ID")
    author: str = Field(description="Comment author")
    content: str = Field(description="Comment text")
    anchor_text: str = Field(default="", description="Text the comment is anchored to")
    replies: list[str] = Field(default_factory=list, description="Reply texts in order")
    resolved: bool = Field(description="Whether the comment is resolved")


class ReadCommentsOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    comments: list[CommentEntry] = Field(description="All comments on the doc")


class UpdateOverviewInput(BaseModel):
    content: str = Field(
        description="Markdown content for the Overview tab (replaces existing)"
    )


class UpdateOverviewOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    status: str = Field(description="Update status")


class ListTabsInput(BaseModel):
    pass


class TabInfo(BaseModel):
    tab_id: str = Field(description="Tab ID")
    title: str = Field(description="Tab title")


class ListTabsOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tabs: list[TabInfo] = Field(description="All tabs in the doc")


class InsertImageInput(BaseModel):
    file_path: str = Field(
        description="Path to image file in /shared/ directory (e.g. '/shared/chart.png')"
    )
    tab_id: str = Field(description="Tab ID to insert the image into")
    alt_text: str = Field(default="", description="Alt text for accessibility")
    width_pts: int = Field(
        default=400,
        description="Image width in points (72pt = 1 inch). Height is computed from aspect ratio.",
    )


class InsertImageOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    drive_file_id: str = Field(description="Google Drive file ID of the uploaded image")
    image_url: str = Field(description="Public URL of the uploaded image")


class RenderEquationInput(BaseModel):
    latex: str = Field(
        description="LaTeX math expression (e.g. 'E = mc^2' or '\\\\frac{a}{b}')"
    )
    tab_id: str = Field(description="Tab ID to insert the rendered equation into")
    display: bool = Field(
        default=False,
        description="True for display mode (centered, larger), False for inline",
    )


class RenderEquationOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    drive_file_id: str = Field(
        description="Google Drive file ID of the rendered equation"
    )
    latex: str = Field(description="The LaTeX expression that was rendered")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_tab_text(tab: object) -> str:
    """Extract plain text from a tab's document content."""
    if not isinstance(tab, dict):
        return ""
    body = tab.get("documentTab", {}).get("body", {})
    if not isinstance(body, dict):
        return ""
    content = body.get("content", [])
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for element in content:
        if not isinstance(element, dict):
            continue
        paragraph = element.get("paragraph")
        if not isinstance(paragraph, dict):
            continue
        for pe in paragraph.get("elements", []):
            if not isinstance(pe, dict):
                continue
            text_run = pe.get("textRun")
            if isinstance(text_run, dict):
                text = text_run.get("content", "")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


HEADING_LEVEL: dict[str, int] = {
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
}


def extract_tab_markdown(tab: object) -> str:
    """Reconstruct markdown from a tab's document content and formatting.

    Reads paragraph styles (headings, bullets) and text styles (bold, italic,
    links) from the Google Docs API response and emits markdown that
    round-trips through markdown_to_requests faithfully.
    """
    if not isinstance(tab, dict):
        return ""
    body = tab.get("documentTab", {}).get("body", {})
    if not isinstance(body, dict):
        return ""
    content = body.get("content", [])
    if not isinstance(content, list):
        return ""

    paragraphs: list[str] = []

    for element in content:
        if not isinstance(element, dict):
            continue
        paragraph = element.get("paragraph")
        if not isinstance(paragraph, dict):
            continue

        para_style = paragraph.get("paragraphStyle", {})
        if not isinstance(para_style, dict):
            para_style = {}
        named_style = para_style.get("namedStyleType", "")

        bullet = paragraph.get("bullet")
        is_bullet = isinstance(bullet, dict)
        is_ordered = False
        if is_bullet and isinstance(bullet, dict):
            list_id = bullet.get("listId", "")
            nesting = bullet.get("nestingLevel", 0)
            if not isinstance(nesting, int):
                nesting = 0
            is_ordered = _is_ordered_list(tab, str(list_id), nesting)

        runs: list[str] = []
        for pe in paragraph.get("elements", []):
            if not isinstance(pe, dict):
                continue
            text_run = pe.get("textRun")
            if not isinstance(text_run, dict):
                continue
            text = text_run.get("content", "")
            if not isinstance(text, str):
                continue

            style = text_run.get("textStyle", {})
            if not isinstance(style, dict):
                style = {}

            chunk = text.rstrip("\n")
            if not chunk:
                continue

            bold = style.get("bold") is True
            italic = style.get("italic") is True
            link = style.get("link", {})
            url = ""
            if isinstance(link, dict):
                url = str(link.get("url", ""))

            if url:
                chunk = f"[{chunk}]({url})"
            if bold and italic:
                chunk = f"***{chunk}***"
            elif bold:
                chunk = f"**{chunk}**"
            elif italic:
                chunk = f"*{chunk}*"

            runs.append(chunk)

        line = "".join(runs)
        if not line:
            paragraphs.append("")
            continue

        heading_level = HEADING_LEVEL.get(str(named_style))
        if heading_level is not None:
            paragraphs.append(f"{'#' * heading_level} {line}")
        elif is_bullet:
            prefix = "1. " if is_ordered else "- "
            paragraphs.append(f"{prefix}{line}")
        else:
            paragraphs.append(line)

    return "\n\n".join(p for p in paragraphs if p is not None).strip() + "\n"


def _is_ordered_list(tab: object, list_id: str, nesting_level: int) -> bool:
    """Check if a list at the given nesting level uses ordered numbering."""
    if not isinstance(tab, dict):
        return False
    lists = tab.get("documentTab", {}).get("lists", {})
    if not isinstance(lists, dict):
        return False
    list_def = lists.get(list_id, {})
    if not isinstance(list_def, dict):
        return False
    props = list_def.get("listProperties", {})
    if not isinstance(props, dict):
        return False
    levels = props.get("nestingLevels", [])
    if not isinstance(levels, list) or nesting_level >= len(levels):
        return False
    level = levels[nesting_level]
    if not isinstance(level, dict):
        return False
    glyph_type = level.get("glyphType", "")
    return isinstance(glyph_type, str) and glyph_type not in (
        "",
        "GLYPH_TYPE_UNSPECIFIED",
    )


def get_tab_end_index(tab: object) -> int:
    """Get the end index of content in a tab (for clearing)."""
    if not isinstance(tab, dict):
        return 1
    body = tab.get("documentTab", {}).get("body", {})
    if not isinstance(body, dict):
        return 1
    content = body.get("content", [])
    if not isinstance(content, list):
        return 1

    end = 1
    for element in content:
        if isinstance(element, dict):
            ei = element.get("endIndex")
            if isinstance(ei, int) and ei > end:
                end = ei
    return end


def search_tabs_recursive(tabs: list[object], tab_id: str) -> tuple[str, object] | None:
    """Recursively search tabs and their children for a tab ID."""
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties", {})
        if isinstance(props, dict) and props.get("tabId") == tab_id:
            return (tab_id, tab)
        children = tab.get("childTabs", [])
        if isinstance(children, list):
            result = search_tabs_recursive(children, tab_id)
            if result is not None:
                return result
    return None


def find_tab_by_id(doc: object, tab_id: str | None) -> tuple[str, object]:
    """Find a tab in a document by ID, including nested tabs."""
    if not isinstance(doc, dict):
        raise ToolError("Invalid document response")

    tabs = doc.get("tabs", [])
    if not isinstance(tabs, list) or not tabs:
        raise ToolError("Document has no tabs")

    if tab_id is None:
        first_tab = tabs[0]
        if isinstance(first_tab, dict):
            props = first_tab.get("tabProperties", {})
            if isinstance(props, dict):
                tid = props.get("tabId", "")
                return (str(tid), first_tab)
        raise ToolError("Could not read first tab")

    result = search_tabs_recursive(tabs, tab_id)
    if result is not None:
        return result

    raise ToolError(f"Tab '{tab_id}' not found in document")


# ---------------------------------------------------------------------------
# Shared operations (called by both MCP tools and pipeline)
# ---------------------------------------------------------------------------


async def do_create_doc(
    title: str,
    share_with: str | None = None,
    session_state: WritingSessionState | None = None,
) -> tuple[str, str]:
    """Create a Google Doc. Returns (doc_id, url).

    Sets default permissions:
    - Anyone with the link can comment
    - Anyone in the Google Workspace domain can edit (if domain configured)
    - Named user gets editor access (if share_with provided)
    """
    from inkwell.agent.config import current_settings

    settings = current_settings()

    svc = services()
    docs = svc.docs_service()
    result = await execute_with_retry(docs.documents().create(body={"title": title}))
    doc_id = result["documentId"]

    drive = svc.drive_service()

    await execute_with_retry(
        drive.permissions().create(
            fileId=doc_id,
            body={"type": "anyone", "role": "commenter"},
            sendNotificationEmail=False,
        )
    )

    if settings.google_workspace_domain:
        await execute_with_retry(
            drive.permissions().create(
                fileId=doc_id,
                body={
                    "type": "domain",
                    "role": "writer",
                    "domain": settings.google_workspace_domain,
                },
                sendNotificationEmail=False,
            )
        )

    if share_with:
        await execute_with_retry(
            drive.permissions().create(
                fileId=doc_id,
                body={
                    "type": "user",
                    "role": "writer",
                    "emailAddress": share_with,
                },
                sendNotificationEmail=False,
            )
        )

    url = f"https://docs.google.com/document/d/{doc_id}/edit"

    if session_state is not None:
        session_state.set_doc(doc_id, url)

    return doc_id, url


async def do_rename_doc(doc_id: str, title: str) -> None:
    """Rename a Google Doc via Drive API."""
    svc = services()
    drive = svc.drive_service()
    await execute_with_retry(drive.files().update(fileId=doc_id, body={"name": title}))


def truncate_tab_title(name: str) -> str:
    """Truncate a tab title to fit Google Docs' limit."""
    MAX_TAB_TITLE = 50
    if len(name) > MAX_TAB_TITLE:
        return name[: MAX_TAB_TITLE - 1] + "…"
    return name


async def find_tab_by_title(doc_id: str, title: str) -> str | None:
    """Find an existing tab by title, returning its ID or None."""
    for tab in await do_list_tabs(doc_id):
        if tab.title == title:
            return tab.tab_id
    return None


async def do_create_tab(
    doc_id: str,
    name: str,
    parent_tab_id: str | None = None,
) -> str:
    """Create a tab in a Google Doc. Returns the tab ID.

    If parent_tab_id is provided, the tab is nested under the parent.
    Idempotent: returns the existing tab ID if a tab with this title exists.
    """
    name = truncate_tab_title(name)
    tab_props: dict[str, str] = {"title": name}
    if parent_tab_id is not None:
        tab_props["parentTabId"] = parent_tab_id
    svc = services()
    docs = svc.docs_service()
    try:
        result = await execute_with_retry(
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"addDocumentTab": {"tabProperties": tab_props}}]},
            )
        )
    except HttpError as exc:
        if exc.resp.status == 400 and "Tab title must be unique" in str(exc):
            tab_id = await find_tab_by_title(doc_id, name)
            if tab_id:
                logger.info("Tab '%s' already exists (id=%s), reusing", name, tab_id)
                return tab_id
        raise
    replies = result.get("replies", [])
    tab_id: str | None = None
    if replies:
        first = replies[0]
        if isinstance(first, dict):
            props = first.get("addDocumentTab", {}).get("tabProperties", {})
            if isinstance(props, dict):
                raw = props.get("tabId")
                if raw:
                    tab_id = str(raw)

    if not tab_id:
        raise ToolError(
            f"Failed to extract tab ID after creating tab '{name}': "
            f"unexpected API response: {result!r}"
        )

    return tab_id


async def do_write_tab(
    doc_id: str,
    tab_id: str,
    markdown: str,
    session_state: WritingSessionState | None = None,
    plain: bool = False,
) -> None:
    """Write content to a tab (replaces existing content).

    ``plain=True`` inserts the text verbatim, with no markdown rendering — for
    raw LaTeX source and other content that must not be reinterpreted.
    """
    svc = services()
    docs = svc.docs_service()

    doc = await execute_with_retry(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    _, tab = find_tab_by_id(doc, tab_id)
    end_index = get_tab_end_index(tab)

    if plain:
        content_requests: list[dict[str, object]] = [
            {
                "insertText": {
                    "location": {"index": 1, "tabId": tab_id},
                    "text": markdown,
                }
            }
        ]
        formatting_requests: list[dict[str, object]] = []
    else:
        all_requests = markdown_to_requests(markdown, tab_id=tab_id)
        batch = split_markdown_batch(all_requests)
        content_requests = batch["content"]
        formatting_requests = batch["formatting"]

    phase1: list[dict[str, object]] = []
    if end_index > 2:
        phase1.append(clear_tab_request(end_index, tab_id=tab_id))
    phase1.extend(content_requests)

    if phase1:
        await execute_with_retry(
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": phase1})
        )

    if formatting_requests:
        doc = await execute_with_retry(
            docs.documents().get(documentId=doc_id, includeTabsContent=True)
        )
        _, tab = find_tab_by_id(doc, tab_id)
        actual_end = get_tab_end_index(tab)
        clamped = clamp_ranges(formatting_requests, actual_end)
        if clamped:
            await execute_with_retry(
                docs.documents().batchUpdate(
                    documentId=doc_id, body={"requests": clamped}
                )
            )

    if session_state is not None:
        for section in session_state.sections:
            if section["tab_id"] == tab_id and section["status"] in (
                "planned",
                "writing",
            ):
                section["status"] = "drafted"
                break


TAB_CONTINUATION_CHARS = 50_000
MAX_CONTINUATION_TABS = 4


async def write_with_continuation(
    doc_id: str,
    tab_name: str,
    content: str,
    parent_tab_id: str | None = None,
    session_state: WritingSessionState | None = None,
    plain: bool = False,
) -> list[str]:
    """Write content to one or more tabs, splitting at heading boundaries when large.

    Under TAB_CONTINUATION_CHARS: writes to a single tab (existing behavior).
    Over: splits at ## heading boundaries into continuation tabs named
    "{tab_name}", "{tab_name} (2/N)", etc.

    Returns the list of tab IDs written to.
    """
    from lup.content_safety import split_on_headings

    if plain or len(content) <= TAB_CONTINUATION_CHARS:
        tab_id = await find_tab_by_title(doc_id, tab_name)
        if tab_id is None:
            tab_id = await do_create_tab(doc_id, tab_name, parent_tab_id)
        await do_write_tab(doc_id, tab_id, content, session_state, plain=plain)
        return [tab_id]

    chunks = split_on_headings(content)

    merged: list[str] = []
    current: list[str] = []
    current_len = 0
    for _heading, chunk_text in chunks:
        if current_len + len(chunk_text) > TAB_CONTINUATION_CHARS and current:
            merged.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(chunk_text)
        current_len += len(chunk_text)
    if current:
        merged.append("\n\n".join(current))

    if len(merged) > MAX_CONTINUATION_TABS:
        dropped = len(merged) - MAX_CONTINUATION_TABS
        logger.warning(
            "Content for '%s' needs %d tabs but max is %d — dropping %d tab(s)",
            tab_name,
            len(merged),
            MAX_CONTINUATION_TABS,
            dropped,
        )
        merged = merged[:MAX_CONTINUATION_TABS]
        merged[-1] += (
            f"\n\n---\n*{dropped} additional section(s) omitted from Google Doc. "
            f"Full content available in local draft files.*"
        )

    total = len(merged)
    tab_ids: list[str] = []

    for i, chunk_content in enumerate(merged):
        if i == 0:
            name = tab_name
        else:
            name = f"{tab_name} ({i + 1}/{total})"

        tab_id = await find_tab_by_title(doc_id, name)
        if tab_id is None:
            tab_id = await do_create_tab(doc_id, name, parent_tab_id)
        await do_write_tab(doc_id, tab_id, chunk_content, session_state)
        tab_ids.append(tab_id)

    return tab_ids


async def do_insert_comment(
    doc_id: str,
    content: str,
    anchor_text: str | None = None,
    session_state: WritingSessionState | None = None,
) -> str:
    """Insert a comment on the doc. Returns comment ID."""
    svc = services()
    drive = svc.drive_service()
    body: dict[str, object] = {"content": content}
    if anchor_text:
        body["quotedFileContent"] = {"mimeType": "text/plain", "value": anchor_text}
    result = await execute_with_retry(
        drive.comments().create(fileId=doc_id, body=body, fields="id")
    )
    comment_id = str(result.get("id", ""))

    if session_state is not None:
        session_state.mark_agent_comment(comment_id)

    return comment_id


class CommentSpec(BaseModel):
    content: str
    anchor_text: str | None = None


async def do_insert_comments_batch(
    doc_id: str,
    comments: list[CommentSpec],
    session_state: WritingSessionState | None = None,
) -> list[str]:
    """Insert multiple comments concurrently. Returns list of comment IDs."""
    if not comments:
        return []

    async def insert_one(spec: CommentSpec) -> str:
        return await do_insert_comment(
            doc_id,
            spec.content,
            anchor_text=spec.anchor_text,
            session_state=session_state,
        )

    results = await asyncio.gather(
        *(insert_one(spec) for spec in comments),
        return_exceptions=True,
    )
    ids: list[str] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Batch comment failed: %s", result)
            ids.append("")
        else:
            ids.append(result)
    return ids


async def do_fetch_comments(
    doc_id: str,
    *,
    exclude_ids: set[str] | None = None,
    include_resolved: bool = False,
) -> list[CommentEntry]:
    """Fetch comments from any Google Doc by ID."""
    svc = services()
    drive = svc.drive_service()
    skip = exclude_ids or set()
    entries: list[CommentEntry] = []
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
        for item in result.get("comments", []):
            if not isinstance(item, dict):
                continue
            if not include_resolved and item.get("resolved", False):
                continue
            comment_id = str(item.get("id", ""))
            if comment_id in skip:
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

            entries.append(
                CommentEntry(
                    comment_id=comment_id,
                    author=author_name,
                    content=str(item.get("content", "")),
                    anchor_text=anchor,
                    replies=replies,
                    resolved=bool(item.get("resolved", False)),
                )
            )

        page_token_raw = result.get("nextPageToken")
        if isinstance(page_token_raw, str) and page_token_raw:
            page_token = page_token_raw
        else:
            break

    return entries


async def do_reply_to_comment(
    doc_id: str,
    comment_id: str,
    reply_text: str,
    session_state: WritingSessionState | None = None,
) -> str:
    """Post a reply to an existing comment. Returns reply ID."""
    svc = services()
    drive = svc.drive_service()
    result = await execute_with_retry(
        drive.replies().create(
            fileId=doc_id,
            commentId=comment_id,
            body={"content": reply_text},
            fields="id",
        )
    )
    reply_id = str(result.get("id", ""))

    if session_state is not None:
        session_state.mark_agent_comment(reply_id)

    return reply_id


async def do_read_tab(
    doc_id: str,
    tab_id: str,
    *,
    accept_suggestions: bool = False,
    as_markdown: bool = False,
) -> str:
    """Read content of a tab. Returns plain text or reconstructed markdown.

    With accept_suggestions=True, reads the doc as if all pending
    suggestions were accepted — detects both direct edits and
    suggestion-mode changes in one read.

    With as_markdown=True, reconstructs markdown from formatting (headings,
    bold, italic, links, lists) instead of returning plain text.
    """
    svc = services()
    docs = svc.docs_service()

    kwargs: dict[str, object] = {
        "documentId": doc_id,
        "includeTabsContent": True,
    }
    if accept_suggestions:
        kwargs["suggestionsViewMode"] = "PREVIEW_SUGGESTIONS_ACCEPTED"

    doc = await execute_with_retry(docs.documents().get(**kwargs))

    _, tab = find_tab_by_id(doc, tab_id)
    if as_markdown:
        return extract_tab_markdown(tab)
    return extract_tab_text(tab)


async def do_insert_image(
    doc_id: str,
    tab_id: str,
    host_path: str,
    width_pts: int = 400,
) -> tuple[str, str]:
    """Upload an image to Drive and insert it into a Google Doc tab.

    Returns (drive_file_id, image_url).
    """
    return await do_insert_image_impl(doc_id, tab_id, host_path, width_pts)


async def do_upload_artifact(
    host_path: str,
    mime_type: str,
) -> tuple[str, str]:
    """Upload a file to Drive with link-sharing. Returns (file_id, view_url)."""
    from pathlib import Path

    from googleapiclient.http import MediaFileUpload

    path = Path(host_path)
    if not path.exists():
        raise ToolError(f"File not found: {host_path}")

    svc = services()
    drive = svc.drive_service()
    file_metadata: dict[str, str] = {"name": path.name, "mimeType": mime_type}
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
    uploaded = await execute_with_retry(
        drive.files().create(body=file_metadata, media_body=media, fields="id")
    )
    file_id: str = uploaded["id"]
    await execute_with_retry(
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            sendNotificationEmail=False,
        )
    )
    return file_id, f"https://drive.google.com/file/d/{file_id}/view"


async def do_insert_image_impl(
    doc_id: str,
    tab_id: str,
    host_path: str,
    width_pts: int = 400,
) -> tuple[str, str]:
    from pathlib import Path

    from PIL import Image

    path = Path(host_path)
    if not path.exists():
        raise ToolError(f"Image file not found: {host_path}")

    with Image.open(path) as img:
        w, h = img.size
    height_pts = int(width_pts * h / w) if w > 0 else width_pts

    drive_file_id, _ = await do_upload_artifact(host_path, "image/png")
    image_url = f"https://drive.google.com/uc?id={drive_file_id}"

    svc = services()
    docs = svc.docs_service()
    doc = await execute_with_retry(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    _, tab = find_tab_by_id(doc, tab_id)
    end_index = get_tab_end_index(tab)
    insert_index = max(1, end_index - 1)

    requests_body: list[dict[str, object]] = [
        {
            "insertInlineImage": {
                "uri": image_url,
                "objectSize": {
                    "width": {"magnitude": width_pts, "unit": "PT"},
                    "height": {"magnitude": height_pts, "unit": "PT"},
                },
                "location": {"index": insert_index, "tabId": tab_id},
            }
        }
    ]

    await execute_with_retry(
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests_body}
        )
    )

    logger.info(
        "Inserted image %s into tab %s (%dx%d pt)",
        path.name,
        tab_id,
        width_pts,
        height_pts,
    )

    return drive_file_id, image_url


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@lup_tool(
    "Create a new Google Doc for a writing session. Use this at the start of "
    "every new article to create the live collaboration surface. The doc is "
    "shared with the author so they can follow progress in real time. "
    "Returns the doc ID and URL."
)
async def create_doc(params: CreateDocInput) -> CreateDocOutput:
    doc_id, url = await do_create_doc(
        params.title,
        params.share_with,
        session_state=get_session_state(),
    )
    return CreateDocOutput(doc_id=doc_id, url=url)


@lup_tool(
    "Create a new tab in an existing Google Doc. Use this to create separate "
    "writing spaces for each section (e.g., '§1 Introduction', '§2 Background'). "
    "Each section writer works in its own tab to avoid conflicts during "
    "parallel writing."
)
async def create_tab(params: CreateTabInput) -> CreateTabOutput:
    doc_id = require_doc_id()
    tab_id = await do_create_tab(doc_id, params.tab_name)
    return CreateTabOutput(doc_id=doc_id, tab_id=tab_id, tab_name=params.tab_name)


@lup_tool(
    "Write markdown content to a specific tab in the Google Doc. Replaces "
    "the entire tab content. The markdown is converted to native Google Docs "
    "formatting (headings, bold, italic, links, lists). For incremental "
    "updates, read first, modify, then write back."
)
async def write_tab(params: WriteTabInput) -> WriteTabOutput:
    doc_id = require_doc_id()
    await do_write_tab(
        doc_id, params.tab_id, params.content, session_state=get_session_state()
    )
    return WriteTabOutput(
        doc_id=doc_id,
        tab_id=params.tab_id,
        characters_written=len(params.content),
    )


@lup_tool(
    "Read the content of a tab in the Google Doc. Saves the tab content "
    "to disk and returns a file path, word count, and preview. Use Read "
    "to access the full text. Use this to read section drafts before "
    "merging, to check what's been written, or to read the current "
    "state of any tab."
)
async def read_tab(params: ReadTabInput) -> ReadTabOutput:
    doc_id = require_doc_id()
    svc = services()
    docs = svc.docs_service()

    doc = await execute_with_retry(
        docs.documents().get(
            documentId=doc_id,
            includeTabsContent=True,
        )
    )

    tab_id, tab = find_tab_by_id(doc, params.tab_id)
    text = extract_tab_text(tab)
    saved = save_content("tab", f"{doc_id}-{tab_id}", text)

    return ReadTabOutput(
        doc_id=doc_id,
        tab_id=tab_id,
        content=saved,
    )


@lup_tool(
    "Insert a comment on the Google Doc. Use this for questions to the author "
    "(e.g., 'Should this use the technical definition?'), for reviewer findings "
    "anchored to specific text, and for any async communication. The author "
    "sees comments in real time and can reply whenever convenient. "
    "Anchor to specific text by providing anchor_text."
)
async def insert_comment(params: InsertCommentInput) -> InsertCommentOutput:
    doc_id = require_doc_id()
    comment_id = await do_insert_comment(
        doc_id,
        params.content,
        params.anchor_text,
        session_state=get_session_state(),
    )
    return InsertCommentOutput(
        doc_id=doc_id,
        comment_id=comment_id,
        content=params.content,
    )


@lup_tool(
    "Read all comments on the Google Doc, including replies. Use this before "
    "the final rewrite to incorporate author feedback, or during any revision "
    "pass. Returns comment text, anchor text, replies, and resolved status."
)
async def read_comments(params: ReadCommentsInput) -> ReadCommentsOutput:
    doc_id = require_doc_id()
    svc = services()
    drive = svc.drive_service()

    comments_list: list[CommentEntry] = []
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
            resolved = bool(item.get("resolved", False))
            if not params.include_resolved and resolved:
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

            comments_list.append(
                CommentEntry(
                    comment_id=str(item.get("id", "")),
                    author=author_name,
                    content=str(item.get("content", "")),
                    anchor_text=anchor,
                    replies=replies,
                    resolved=resolved,
                )
            )

        page_token_raw = result.get("nextPageToken")
        if isinstance(page_token_raw, str) and page_token_raw:
            page_token = page_token_raw
        else:
            break

    return ReadCommentsOutput(
        doc_id=doc_id,
        comments=comments_list,
    )


@lup_tool(
    "Update the Overview tab of the Google Doc with current progress. Use this "
    "to keep the author informed about pipeline status: which sections are "
    "being written, which are done, what questions are pending. The Overview "
    "tab is the author's dashboard for the writing session."
)
async def update_overview(params: UpdateOverviewInput) -> UpdateOverviewOutput:
    doc_id = require_doc_id()
    svc = services()
    docs = svc.docs_service()

    doc = await execute_with_retry(
        docs.documents().get(
            documentId=doc_id,
            includeTabsContent=True,
        )
    )

    tabs = doc.get("tabs", [])
    overview_tab_id: str | None = None

    if isinstance(tabs, list):
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            props = tab.get("tabProperties", {})
            if isinstance(props, dict):
                title = props.get("title", "")
                if isinstance(title, str) and title.lower() in ("overview", ""):
                    overview_tab_id = str(props.get("tabId", ""))
                    _, matched_tab = find_tab_by_id(doc, overview_tab_id)
                    end_index = get_tab_end_index(matched_tab)

                    all_reqs = markdown_to_requests(
                        params.content, tab_id=overview_tab_id
                    )
                    ov_batch = split_markdown_batch(all_reqs)

                    phase1: list[dict[str, object]] = []
                    if end_index > 2:
                        phase1.append(
                            clear_tab_request(end_index, tab_id=overview_tab_id)
                        )
                    phase1.extend(ov_batch["content"])
                    if phase1:
                        await execute_with_retry(
                            docs.documents().batchUpdate(
                                documentId=doc_id,
                                body={"requests": phase1},
                            )
                        )

                    if ov_batch["formatting"]:
                        doc = await execute_with_retry(
                            docs.documents().get(
                                documentId=doc_id, includeTabsContent=True
                            )
                        )
                        _, matched_tab = find_tab_by_id(doc, overview_tab_id)
                        actual_end = get_tab_end_index(matched_tab)
                        clamped = clamp_ranges(ov_batch["formatting"], actual_end)
                        if clamped:
                            await execute_with_retry(
                                docs.documents().batchUpdate(
                                    documentId=doc_id,
                                    body={"requests": clamped},
                                )
                            )
                    break

    if overview_tab_id is None:
        raise ToolError("No Overview tab found. Create one first with create_tab.")

    return UpdateOverviewOutput(
        doc_id=doc_id,
        status="updated",
    )


@lup_tool(
    "List all tabs in a Google Doc, including nested child tabs. Use this "
    "to discover available section tabs, check what's been created, or "
    "find the tab IDs needed for read/write operations."
)
async def list_tabs(_params: ListTabsInput) -> ListTabsOutput:
    doc_id = require_doc_id()
    tab_list = await do_list_tabs(doc_id)
    return ListTabsOutput(doc_id=doc_id, tabs=tab_list)


def collect_all_tabs(tabs: list[object]) -> list[TabInfo]:
    """Recursively collect TabInfo from tabs and their children."""
    result: list[TabInfo] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties", {})
        if isinstance(props, dict):
            result.append(
                TabInfo(
                    tab_id=str(props.get("tabId", "")),
                    title=str(props.get("title", "")),
                )
            )
        children = tab.get("childTabs", [])
        if isinstance(children, list):
            result.extend(collect_all_tabs(children))
    return result


async def do_list_tabs(doc_id: str) -> list[TabInfo]:
    """List all tabs in a document, including nested children."""
    svc = services()
    docs = svc.docs_service()
    doc = await execute_with_retry(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    tabs_raw = doc.get("tabs", [])
    if isinstance(tabs_raw, list):
        return collect_all_tabs(tabs_raw)
    return []


GDOC_URL_RE = re.compile(r"https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")


class AttachDocInput(BaseModel):
    url: str = Field(
        description="Google Doc URL (https://docs.google.com/document/d/<id>/...)"
    )


class AttachDocOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    url: str = Field(description="Google Doc URL")
    title: str = Field(description="Document title")
    tabs: list[TabInfo] = Field(description="Existing tabs in the doc")
    overview_tab_id: str = Field(
        default="", description="Overview tab ID (created if missing)"
    )
    directions_tab_id: str = Field(
        default="", description="Directions tab ID (created if missing)"
    )


@lup_tool(
    "Attach to an existing Google Doc instead of creating a new one. Use when "
    "the author provides a Google Doc URL as the starting seed. Creates an "
    "Overview tab and Directions tab if they don't exist. Returns all tabs "
    "so you can read their content with read_tab."
)
async def attach_doc(params: AttachDocInput) -> AttachDocOutput:
    match = GDOC_URL_RE.search(params.url)
    if not match:
        raise ToolError(
            f"Not a Google Doc URL: {params.url}. "
            "Expected: https://docs.google.com/document/d/<doc_id>/..."
        )
    doc_id = match.group(1)

    svc = services()
    docs = svc.docs_service()

    doc = await execute_with_retry(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    doc_title = str(doc.get("title", "Untitled"))

    tabs_raw = doc.get("tabs", [])
    if not isinstance(tabs_raw, list):
        raise ToolError("Could not read document tabs")

    existing_tabs: list[TabInfo] = []
    overview_tab_id = ""
    directions_tab_id = ""

    for tab in tabs_raw:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties", {})
        if not isinstance(props, dict):
            continue
        tid = str(props.get("tabId", ""))
        title = str(props.get("title", ""))
        existing_tabs.append(TabInfo(tab_id=tid, title=title))
        if title.lower() == "overview":
            overview_tab_id = tid
        elif title.lower() == "directions":
            directions_tab_id = tid

    for tab_name in ("Overview", "Directions"):
        is_overview = tab_name == "Overview"
        if is_overview and overview_tab_id:
            continue
        if not is_overview and directions_tab_id:
            continue

        result = await execute_with_retry(
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"addTab": {"tabProperties": {"title": tab_name}}}]},
            )
        )
        replies = result.get("replies", [])
        if replies and isinstance(replies[0], dict):
            add_tab = replies[0].get("addTab", {})
            if isinstance(add_tab, dict):
                tab_props = add_tab.get("tabProperties", {})
                if isinstance(tab_props, dict):
                    new_tid = str(tab_props.get("tabId", ""))
                    existing_tabs.append(TabInfo(tab_id=new_tid, title=tab_name))
                    if is_overview:
                        overview_tab_id = new_tid
                    else:
                        directions_tab_id = new_tid

    url = f"https://docs.google.com/document/d/{doc_id}/edit"

    state = get_session_state()
    if state is not None:
        state.set_doc(doc_id, url)
        state.directions_tab_id = directions_tab_id

    return AttachDocOutput(
        doc_id=doc_id,
        url=url,
        title=doc_title,
        tabs=existing_tabs,
        overview_tab_id=overview_tab_id,
        directions_tab_id=directions_tab_id,
    )


class ReadDirectionsInput(BaseModel):
    pass


class ReadDirectionsOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    content: str = Field(description="Current content of the Directions tab")
    new_content: str = Field(
        description="Content added since last read (empty if no changes)"
    )
    has_changes: bool = Field(description="Whether new content was found")


@lup_tool(
    "Read the Directions tab where the author types commands and guidance. "
    "Returns new content since last read, so you can react to changes. "
    "Use at stage boundaries alongside check_author_feedback for full "
    "author input."
)
async def read_directions(_params: ReadDirectionsInput) -> ReadDirectionsOutput:
    doc_id = require_doc_id()

    state = get_session_state()
    directions_tab_id = state.directions_tab_id if state else ""

    if not directions_tab_id:
        raise ToolError(
            "No Directions tab found. Use attach_doc to set up the doc, "
            "or create a 'Directions' tab with create_tab."
        )

    svc = services()
    docs = svc.docs_service()

    doc = await execute_with_retry(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )

    _, tab = find_tab_by_id(doc, directions_tab_id)
    content = extract_tab_text(tab)

    new_content = ""
    has_changes = False
    if state is not None:
        last = state.last_directions_content
        if content != last:
            has_changes = True
            if last and content.startswith(last):
                new_content = content[len(last) :]
            else:
                new_content = content
            state.last_directions_content = content
    else:
        new_content = content
        has_changes = bool(content.strip())

    return ReadDirectionsOutput(
        doc_id=doc_id,
        content=content,
        new_content=new_content.strip(),
        has_changes=has_changes,
    )


@lup_tool(
    "Insert an image from the /shared/ directory into a Google Doc tab. The image "
    "must already exist on disk — create it using execute_code (matplotlib, PIL, etc.) "
    "and save to /shared/. The tool uploads the image to Google Drive and inserts it "
    "inline at the end of the tab content. Use for charts, diagrams, rendered "
    "equations, or any visual content."
)
async def insert_image(params: InsertImageInput) -> InsertImageOutput:
    doc_id = require_doc_id()

    state = get_session_state()
    if state is None or state.shared_dir is None:
        raise ToolError(
            "Sandbox not available — cannot resolve /shared/ path. "
            "Images must be created via execute_code in the sandbox."
        )
    relative = params.file_path.lstrip("/")
    if relative.startswith("shared/"):
        relative = relative[len("shared/") :]
    host_path = str(state.shared_dir / relative)

    drive_file_id, image_url = await do_insert_image(
        doc_id,
        params.tab_id,
        host_path,
        width_pts=params.width_pts,
    )
    return InsertImageOutput(
        doc_id=doc_id,
        drive_file_id=drive_file_id,
        image_url=image_url,
    )


def render_latex_to_png(latex: str, output_path: str, *, display: bool = False) -> None:
    """Render a LaTeX expression to a PNG file using matplotlib's mathtext."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fontsize = 16 if display else 13
    fig, ax = plt.subplots(figsize=(0.01, 0.01))
    ax.axis("off")
    wrapped = f"${latex}$" if not latex.startswith("$") else latex
    ax.text(
        0,
        0,
        wrapped,
        fontsize=fontsize,
        color="black",
        verticalalignment="center",
    )
    fig.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
        transparent=False,
        pad_inches=0.05,
        facecolor="white",
    )
    plt.close(fig)


@lup_tool(
    "Render a LaTeX math expression and insert it as an image in a Google Doc tab. "
    "Use for equations, formulas, and mathematical notation in Google Docs output. "
    "For markdown-native formats (LessWrong, blog), use $...$ notation directly in "
    "the text instead — those platforms render LaTeX natively via KaTeX."
)
async def render_equation(params: RenderEquationInput) -> RenderEquationOutput:
    import hashlib
    from pathlib import Path

    doc_id = require_doc_id()

    state = get_session_state()
    if state is None or state.shared_dir is None:
        raise ToolError(
            "Sandbox not available — cannot render equations without /shared/ directory."
        )

    eq_hash = hashlib.md5(params.latex.encode()).hexdigest()[:8]
    filename = f"eq_{eq_hash}.png"
    host_path = str(state.shared_dir / filename)

    render_latex_to_png(params.latex, host_path, display=params.display)

    if not Path(host_path).exists():
        raise ToolError(f"Failed to render equation: {params.latex}")

    width = 200 if params.display else 120
    drive_file_id, _ = await do_insert_image(
        doc_id,
        params.tab_id,
        host_path,
        width_pts=width,
    )

    return RenderEquationOutput(
        doc_id=doc_id,
        drive_file_id=drive_file_id,
        latex=params.latex,
    )


GOOGLE_DOCS_TOOLS = [
    create_doc,
    create_tab,
    write_tab,
    read_tab,
    insert_comment,
    read_comments,
    update_overview,
    list_tabs,
    attach_doc,
    read_directions,
    insert_image,
    render_equation,
]
