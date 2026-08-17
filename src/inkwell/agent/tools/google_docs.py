"""Google Docs MCP tools for live document collaboration.

The agent writes into a Google Doc that the author follows in real time.
Uses tabs for parallel section writing and comments for async communication.

Requires Google OAuth credentials configured via `inkwell setup`.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Iterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field, ValidationError

from inkwell.agent.google_auth import (
    CommentQuery,
    DocumentQuery,
    GoogleAuthError,
    GoogleRequest,
    ServiceFactory,
    get_service_factory,
)
from inkwell.agent.tools.google_docs_schema import (
    BatchUpdateResponse,
    Comment,
    CommentPage,
    CreatedComment,
    Document,
    Paragraph,
    ParagraphElement,
    Tab,
    UploadedFile,
)
from inkwell.agent.book_links import BookReferences, raised
from inkwell.agent.config import book_store
from inkwell.agent.markdown_to_docs import (
    DocsRequest,
    InsertInlineImage,
    LocationWithTab,
    Magnitude,
    NewTabProperties,
    ObjectSize,
    clamp_ranges,
    clear_tab_request,
    markdown_to_requests,
    split_markdown_batch,
)
from lup.workspace.content_safety import SavedContent, save_content
from lup.mcp import ToolError, lup_tool
from lup.types import JsonObject, JsonValue

if TYPE_CHECKING:
    from inkwell.agent.session import WritingSessionState

logger = logging.getLogger(__name__)

SESSION_STATE_VAR: contextvars.ContextVar[WritingSessionState | None] = (
    contextvars.ContextVar("session_state", default=None)
)


def configure_session_state(state: WritingSessionState) -> None:
    SESSION_STATE_VAR.set(state)


def get_session_state() -> WritingSessionState | None:
    return SESSION_STATE_VAR.get()


@cache
def services() -> ServiceFactory:
    """The Google service factory, built once per process on first use."""
    try:
        return get_service_factory()
    except RuntimeError as e:
        raise ToolError(str(e)) from e


def require_doc_id() -> str:
    """Get doc_id from session state. Raises ToolError if no doc exists."""
    state = get_session_state()
    if state is None or not state.doc_id:
        raise ToolError("No Google Doc created yet. Use create_doc first.")
    return state.doc_id


RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


async def execute_with_retry(
    request: GoogleRequest,
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
) -> JsonValue:
    """Execute a Google API request with retry on transient errors."""
    for attempt in range(max_attempts):
        try:
            return request.execute()
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


async def fetch_document(request: GoogleRequest) -> Document:
    """Execute a document request and read the response into its shape."""
    return Document.model_validate(await execute_with_retry(request))


DRIVE_FAILURES = (HttpError, OSError, TimeoutError, ToolError, GoogleAuthError)
"""What a Google call raises when the service cannot answer it.

Named once because two boundaries catch the same set for different reasons — a
write degrades the live Doc to stale, a comment read reports itself unreachable
— and a set spelled at each of them drifts apart.
"""

COMMENT_READ_FAILURES = (*DRIVE_FAILURES, ValidationError)
"""What a comment read raises when it cannot hand back the comments.

A payload that does not validate belongs here: to a caller waiting on the
author's feedback an unreadable answer is the same event as no answer, and
reading it as an empty page would report the author as silent.
"""


@asynccontextmanager
async def gdoc_nonfatal(operation: str) -> AsyncGenerator[None]:
    """Wrap GDoc calls so failures log a warning instead of crashing the pipeline.

    The Google Doc is a display surface — the pipeline snapshot holds the real
    data.  If a write/comment/tab-create fails, the author sees a stale Doc
    but the pipeline keeps running.  A revoked or expired OAuth token surfaces
    as GoogleAuthError; it is caught here too, so an auth lapse degrades the
    live Doc to stale rather than killing a run whose real output is on disk.
    """
    try:
        yield
    except DRIVE_FAILURES as exc:
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


def extract_tab_text(tab: Tab) -> str:
    """Extract plain text from a tab's document content."""
    return "".join(
        element.text_run.content
        for paragraph in tab.paragraphs()
        for element in paragraph.elements
        if element.text_run is not None
    )


def styled_run(element: ParagraphElement) -> str:
    """One run of text wrapped in the markdown its style calls for."""
    if element.text_run is None:
        return ""
    chunk = element.text_run.content.removesuffix("\n")
    if not chunk:
        return ""
    style = element.text_run.text_style
    if style.link.url:
        chunk = f"[{chunk}]({style.link.url})"
    if style.bold and style.italic:
        return f"***{chunk}***"
    if style.bold:
        return f"**{chunk}**"
    if style.italic:
        return f"*{chunk}*"
    return chunk


def paragraph_markdown(tab: Tab, paragraph: Paragraph) -> str:
    """One paragraph as the markdown line it round-trips to."""
    line = "".join(styled_run(element) for element in paragraph.elements)
    if not line:
        return ""
    heading_level = paragraph.paragraph_style.heading_level()
    if heading_level is not None:
        return f"{'#' * heading_level} {line}"
    if paragraph.bullet is not None:
        prefix = "1. " if tab.ordered_list(paragraph.bullet) else "- "
        return f"{prefix}{line}"
    return line


def extract_tab_markdown(tab: Tab) -> str:
    """Reconstruct markdown from a tab's document content and formatting.

    Reads paragraph styles (headings, bullets) and text styles (bold, italic,
    links) from the Google Docs API response and emits markdown that
    round-trips through markdown_to_requests faithfully.
    """
    return (
        "\n\n".join(
            paragraph_markdown(tab, paragraph) for paragraph in tab.paragraphs()
        ).strip()
        + "\n"
    )


def get_tab_end_index(tab: Tab) -> int:
    """Get the end index of content in a tab (for clearing)."""
    return tab.end_index()


def find_tab_by_id(doc: Document, tab_id: str | None) -> Tab:
    """Find a tab in a document by ID, including nested tabs.

    The tab carries its own id, so a caller that needs one reads it back off
    the tab rather than being handed the pair.
    """
    if not doc.tabs:
        raise ToolError("Document has no tabs")

    if tab_id is None:
        return doc.tabs[0]

    for tab in doc.tabs:
        found = tab.find(tab_id)
        if found is not None:
            return found

    raise ToolError(f"Tab '{tab_id}' not found in document")


# ---------------------------------------------------------------------------
# Shared operations (called by both MCP tools and pipeline)
# ---------------------------------------------------------------------------


class CreatedDoc(BaseModel):
    """A document this session created, and where the author reads it."""

    doc_id: str
    url: str


class DriveUpload(BaseModel):
    """A file uploaded to Drive, and where it can be viewed."""

    file_id: str
    url: str


async def do_create_doc(
    title: str,
    share_with: str | None = None,
    session_state: WritingSessionState | None = None,
) -> CreatedDoc:
    """Create a Google Doc.

    Sets default permissions:
    - Anyone with the link can comment
    - Anyone in the Google Workspace domain can edit (if domain configured)
    - Named user gets editor access (if share_with provided)
    """
    from inkwell.agent.config import current_settings

    settings = current_settings()

    svc = services()
    docs = svc.docs_service()
    created = await fetch_document(docs.documents().create(body={"title": title}))
    doc_id = created.document_id

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

    return CreatedDoc(doc_id=doc_id, url=url)


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
    tab_props: NewTabProperties = {"title": name}
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
    tab_id = BatchUpdateResponse.model_validate(result).created_tab_id() or None

    if not tab_id:
        raise ToolError(
            f"Failed to extract tab ID after creating tab '{name}': "
            f"unexpected API response: {result!r}"
        )

    return tab_id


async def deliverable_requests(markdown: str, tab_id: str | None) -> list[DocsRequest]:
    """This markdown as the requests that write it, its book references resolved.

    The one seam where inkwell renders its own deliverable, and therefore where
    a reference into the book the run is writing becomes a link to a published
    page. Every tab is rendered through here, so a reference resolves the same
    way and an unresolved one is marked and raised the same way whichever tab
    it was written into, rather than however its call site remembered to.

    Which book is read from the session rather than taken as an argument:
    there are many callers and one book, and a caller that forgot to hand it
    along would leave that tab's references silently unresolved. The record is
    re-read per write because the run is writing into that same book — a
    chapter numbered since the last write resolves at this one.

    The author meets an unresolved reference twice, which is what was asked
    for: a marker in the prose, put there by the walk, and a comment raised
    here against the same words. Raised once per target for the whole session,
    so rewriting a tab does not file the same note again — but only once the
    comment is actually filed, because the ledger outlives the run and a
    target marked before a Drive failure would never be raised at all. One
    call per target rather than a batch, so the ledger can wait on each.
    """
    state = get_session_state()
    record = book_store().load(state.book) if state is not None and state.book else None
    references = BookReferences(record=record)
    requests = markdown_to_requests(markdown, tab_id=tab_id, references=references)
    if state is None or not state.doc_id:
        return requests
    for reference in references.unresolved:
        spelled = reference.target.spelled()
        if spelled in state.raised_references:
            continue
        state.raised_references.claim([spelled])
        async with gdoc_nonfatal(f"raise unresolved reference {spelled}"):
            with state.raised_references.recording(spelled):
                await do_insert_comment(
                    state.doc_id,
                    raised(reference.target),
                    anchor_text=reference.text,
                    session_state=state,
                )
    return requests


async def do_write_tab(
    doc_id: str,
    tab_id: str,
    markdown: str,
    session_state: WritingSessionState | None = None,
    plain: bool = False,
) -> None:
    """Write content to a tab (replaces existing content).

    ``plain=True`` inserts the text verbatim, with no markdown rendering — for
    raw LaTeX source and other content that must not be reinterpreted, and so
    also with no reference resolution, since nothing in it is markdown to
    resolve.
    """
    svc = services()
    docs = svc.docs_service()

    doc = await fetch_document(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    tab = find_tab_by_id(doc, tab_id)
    end_index = get_tab_end_index(tab)

    if plain:
        content_requests: list[DocsRequest] = [
            {
                "insertText": {
                    "location": {"index": 1, "tabId": tab_id},
                    "text": markdown,
                }
            }
        ]
        formatting_requests: list[DocsRequest] = []
    else:
        all_requests = await deliverable_requests(markdown, tab_id)
        batch = split_markdown_batch(all_requests)
        content_requests = batch["content"]
        formatting_requests = batch["formatting"]

    phase1: list[DocsRequest] = []
    if end_index > 2:
        phase1.append(clear_tab_request(end_index, tab_id=tab_id))
    phase1.extend(content_requests)

    if phase1:
        await execute_with_retry(
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": phase1})
        )

    if formatting_requests:
        doc = await fetch_document(
            docs.documents().get(documentId=doc_id, includeTabsContent=True)
        )
        tab = find_tab_by_id(doc, tab_id)
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
    from lup.workspace.content_safety import split_on_headings

    if plain or len(content) <= TAB_CONTINUATION_CHARS:
        tab_id = await find_tab_by_title(doc_id, tab_name)
        if tab_id is None:
            tab_id = await do_create_tab(doc_id, tab_name, parent_tab_id)
        await do_write_tab(doc_id, tab_id, content, session_state, plain=plain)
        return [tab_id]

    chunks = split_on_headings(content)

    def packed() -> Iterator[str]:
        """Group the sections into tab-sized chunks, greedily and in order."""
        current = ""
        for section in chunks:
            if current and len(current) + len(section.text) > TAB_CONTINUATION_CHARS:
                yield current
                current = ""
            current = f"{current}\n\n{section.text}" if current else section.text
        if current:
            yield current

    merged = list(packed())

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

    async def written_tab(index: int, chunk_content: str) -> str:
        """Write one chunk into its tab, creating the tab if it is not there."""
        name = tab_name if index == 0 else f"{tab_name} ({index + 1}/{total})"
        tab_id = await find_tab_by_title(doc_id, name)
        if tab_id is None:
            tab_id = await do_create_tab(doc_id, name, parent_tab_id)
        await do_write_tab(doc_id, tab_id, chunk_content, session_state)
        return tab_id

    return [
        await written_tab(index, chunk_content)
        for index, chunk_content in enumerate(merged)
    ]


async def do_insert_comment(
    doc_id: str,
    content: str,
    anchor_text: str | None = None,
    session_state: WritingSessionState | None = None,
) -> str:
    """Insert a comment on the doc. Returns comment ID."""
    svc = services()
    drive = svc.drive_service()
    body: JsonObject = {"content": content}
    if anchor_text:
        body["quotedFileContent"] = {"mimeType": "text/plain", "value": anchor_text}
    result = await execute_with_retry(
        drive.comments().create(fileId=doc_id, body=body, fields="id")
    )
    comment_id = CreatedComment.model_validate(result).id

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

    def collected() -> Iterator[str]:
        """Each insert's comment id, empty for one that failed and was logged."""
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Batch comment failed: %s", result)
                yield ""
            else:
                yield result

    return list(collected())


COMMENT_PAGE_SIZE = 100
"""How many comments to ask Drive for at once, which is its own maximum."""

COMMENT_FIELDS = (
    "comments(id,content,author/displayName,quotedFileContent/value,"
    "replies(id,content),resolved),nextPageToken"
)
"""Every comment field this project reads, and the token that pages through them.

One list rather than one per caller: a poller that omits a field its own
filter needs — a reply's id, the resolved flag — reads a document that looks
empty of exactly the comments it was watching for.
"""


async def fetch_all_comments(
    doc_id: str,
    *,
    fields: str = COMMENT_FIELDS,
    page_size: int = COMMENT_PAGE_SIZE,
) -> list[Comment]:
    """Every comment on a document, page by page through to the last one.

    Drive answers a page at a time and says there is more with
    ``nextPageToken``. A read of the first page alone loses everything past
    it without saying so, which on a document several reviewers have been
    through is most of the feedback — so this is the only place a comment
    read is issued, and it follows the token to the end.
    """
    svc = services()
    drive = svc.drive_service()

    async def paged() -> AsyncIterator[Comment]:
        """Each page's comments, following the token Drive hands back."""
        page_token = ""
        while True:
            query: CommentQuery = {
                "fileId": doc_id,
                "fields": fields,
                "pageSize": page_size,
            }
            if page_token:
                query["pageToken"] = page_token

            page = CommentPage.model_validate(
                await execute_with_retry(drive.comments().list(**query))
            )
            for comment in page.comments:
                yield comment

            if not page.next_page_token:
                return
            page_token = page.next_page_token

    return [comment async for comment in paged()]


async def do_fetch_comments(
    doc_id: str,
    *,
    exclude_ids: Collection[str] = (),
    include_resolved: bool = False,
) -> list[CommentEntry]:
    """Fetch comments from any Google Doc by ID."""
    return [
        CommentEntry(
            comment_id=comment.id,
            author=comment.author.display_name,
            content=comment.content,
            anchor_text=comment.anchor_text(),
            replies=[reply.content for reply in comment.replies if reply.content],
            resolved=comment.resolved,
        )
        for comment in await fetch_all_comments(doc_id)
        if (include_resolved or not comment.resolved) and comment.id not in exclude_ids
    ]


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
    reply_id = CreatedComment.model_validate(result).id

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

    kwargs: DocumentQuery = {
        "documentId": doc_id,
        "includeTabsContent": True,
    }
    if accept_suggestions:
        kwargs["suggestionsViewMode"] = "PREVIEW_SUGGESTIONS_ACCEPTED"

    doc = await fetch_document(docs.documents().get(**kwargs))

    tab = find_tab_by_id(doc, tab_id)
    if as_markdown:
        return extract_tab_markdown(tab)
    return extract_tab_text(tab)


async def do_insert_image(
    doc_id: str,
    tab_id: str,
    host_path: str,
    width_pts: int = 400,
) -> DriveUpload:
    """Upload an image to Drive and insert it into a Google Doc tab.

    Returns (drive_file_id, image_url).
    """
    return await do_insert_image_impl(doc_id, tab_id, host_path, width_pts)


async def do_upload_artifact(
    host_path: str,
    mime_type: str,
) -> DriveUpload:
    """Upload a file to Drive with link-sharing."""
    from pathlib import Path

    from googleapiclient.http import MediaFileUpload

    path = Path(host_path)
    if not path.exists():
        raise ToolError(f"File not found: {host_path}")

    svc = services()
    drive = svc.drive_service()
    file_metadata: JsonObject = {"name": path.name, "mimeType": mime_type}
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
    uploaded = await execute_with_retry(
        drive.files().create(body=file_metadata, media_body=media, fields="id")
    )
    file_id = UploadedFile.model_validate(uploaded).id
    await execute_with_retry(
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            sendNotificationEmail=False,
        )
    )
    return DriveUpload(
        file_id=file_id, url=f"https://drive.google.com/file/d/{file_id}/view"
    )


async def do_insert_image_impl(
    doc_id: str,
    tab_id: str,
    host_path: str,
    width_pts: int = 400,
) -> DriveUpload:
    from pathlib import Path

    from PIL import Image

    path = Path(host_path)
    if not path.exists():
        raise ToolError(f"Image file not found: {host_path}")

    with Image.open(path) as img:
        w, h = img.size
    height_pts = int(width_pts * h / w) if w > 0 else width_pts

    drive_file_id = (await do_upload_artifact(host_path, "image/png")).file_id
    image_url = f"https://drive.google.com/uc?id={drive_file_id}"

    svc = services()
    docs = svc.docs_service()
    doc = await fetch_document(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    tab = find_tab_by_id(doc, tab_id)
    end_index = get_tab_end_index(tab)
    insert_index = max(1, end_index - 1)

    requests_body = [
        DocsRequest(
            insertInlineImage=InsertInlineImage(
                uri=image_url,
                objectSize=ObjectSize(
                    width=Magnitude(magnitude=width_pts, unit="PT"),
                    height=Magnitude(magnitude=height_pts, unit="PT"),
                ),
                location=LocationWithTab(index=insert_index, tabId=tab_id),
            )
        )
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

    return DriveUpload(file_id=drive_file_id, url=image_url)


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
    created = await do_create_doc(
        params.title,
        params.share_with,
        session_state=get_session_state(),
    )
    return CreateDocOutput(doc_id=created.doc_id, url=created.url)


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

    doc = await fetch_document(
        docs.documents().get(
            documentId=doc_id,
            includeTabsContent=True,
        )
    )

    tab = find_tab_by_id(doc, params.tab_id)
    tab_id = tab.tab_properties.tab_id
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
    comments_list = await do_fetch_comments(
        doc_id, include_resolved=params.include_resolved
    )

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

    doc = await fetch_document(
        docs.documents().get(
            documentId=doc_id,
            includeTabsContent=True,
        )
    )

    overview_tab_id = next(
        (
            tab.tab_properties.tab_id
            for tab in doc.tabs
            if tab.tab_properties.title.lower() in ("overview", "")
        ),
        None,
    )
    if overview_tab_id is None:
        raise ToolError("No Overview tab found. Create one first with create_tab.")

    end_index = get_tab_end_index(find_tab_by_id(doc, overview_tab_id))
    ov_batch = split_markdown_batch(
        await deliverable_requests(params.content, overview_tab_id)
    )

    phase1: list[DocsRequest] = []
    if end_index > 2:
        phase1.append(clear_tab_request(end_index, tab_id=overview_tab_id))
    phase1.extend(ov_batch["content"])
    if phase1:
        await execute_with_retry(
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": phase1})
        )

    if ov_batch["formatting"]:
        doc = await fetch_document(
            docs.documents().get(documentId=doc_id, includeTabsContent=True)
        )
        actual_end = get_tab_end_index(find_tab_by_id(doc, overview_tab_id))
        clamped = clamp_ranges(ov_batch["formatting"], actual_end)
        if clamped:
            await execute_with_retry(
                docs.documents().batchUpdate(
                    documentId=doc_id, body={"requests": clamped}
                )
            )

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


def collect_all_tabs(doc: Document) -> list[TabInfo]:
    """Every tab in the document, parents before the tabs nested in them."""
    return [
        TabInfo(tab_id=tab.tab_properties.tab_id, title=tab.tab_properties.title)
        for tab in doc.walk()
    ]


def tab_id_titled(tabs: list[TabInfo], title: str) -> str:
    """The tab titled `title`, case-insensitively, or empty where none is.

    Tab titles are whatever the author named them, so a document without the
    tab a caller is looking for is ordinary rather than exceptional.
    """
    lowered = title.lower()
    return next((tab.tab_id for tab in tabs if tab.title.lower() == lowered), "")


async def do_list_tabs(doc_id: str) -> list[TabInfo]:
    """List all tabs in a document, including nested children."""
    svc = services()
    docs = svc.docs_service()
    doc = await fetch_document(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    return collect_all_tabs(doc)


GDOC_HOST = "docs.google.com"
GDOC_PATH_PREFIX = ("/", "document", "d")


def doc_id_from_url(url: str) -> str | None:
    """The document id a Google Docs URL names, or nothing if it names none.

    The id is a path segment, so the URL is read as one: a hand-rolled pattern
    over the whole string would also match a link that merely mentions the
    address inside a query string or fragment.
    """
    parsed = urlparse(url)
    if parsed.hostname != GDOC_HOST:
        return None
    segments = PurePosixPath(parsed.path).parts
    if segments[: len(GDOC_PATH_PREFIX)] != GDOC_PATH_PREFIX:
        return None
    remainder = segments[len(GDOC_PATH_PREFIX) :]
    return remainder[0] if remainder else None


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
    doc_id = doc_id_from_url(params.url)
    if doc_id is None:
        raise ToolError(
            f"Not a Google Doc URL: {params.url}. "
            "Expected: https://docs.google.com/document/d/<doc_id>/..."
        )

    svc = services()
    docs = svc.docs_service()

    doc = await fetch_document(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )
    doc_title = doc.title or "Untitled"

    existing_tabs = [
        TabInfo(tab_id=tab.tab_properties.tab_id, title=tab.tab_properties.title)
        for tab in doc.tabs
    ]
    overview_tab_id = tab_id_titled(existing_tabs, "overview")
    directions_tab_id = tab_id_titled(existing_tabs, "directions")

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
        new_tid = BatchUpdateResponse.model_validate(result).created_tab_id()
        if new_tid:
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

    doc = await fetch_document(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )

    tab = find_tab_by_id(doc, directions_tab_id)
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
    supplied = PurePosixPath(params.file_path)
    parts = supplied.parts[1:] if supplied.is_absolute() else supplied.parts
    if parts[:1] == ("shared",):
        parts = parts[1:]
    host_path = str(state.shared_dir.joinpath(*parts))

    inserted = await do_insert_image(
        doc_id,
        params.tab_id,
        host_path,
        width_pts=params.width_pts,
    )
    return InsertImageOutput(
        doc_id=doc_id,
        drive_file_id=inserted.file_id,
        image_url=inserted.url,
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
    inserted = await do_insert_image(
        doc_id,
        params.tab_id,
        host_path,
        width_pts=width,
    )

    return RenderEquationOutput(
        doc_id=doc_id,
        drive_file_id=inserted.file_id,
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
