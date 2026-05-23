"""Google Docs MCP tools for live document collaboration.

The agent writes into a Google Doc that the author follows in real time.
Uses tabs for parallel section writing and comments for async communication.

Requires Google OAuth credentials configured via `inkwell setup`.
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false
# googleapiclient returns untyped Resource objects throughout.

import logging

from pydantic import BaseModel, Field

from inkwell.agent.google_auth import ServiceFactory, get_service_factory
from inkwell.agent.markdown_to_docs import clear_tab_request, markdown_to_requests
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

SERVICES: ServiceFactory | None = None


def services() -> ServiceFactory:
    global SERVICES  # noqa: PLW0603
    if SERVICES is None:
        try:
            SERVICES = get_service_factory()
        except RuntimeError as e:
            raise ToolError(str(e)) from e
    return SERVICES


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
    doc_id: str = Field(description="Google Doc ID")
    tab_name: str = Field(description="Name for the new tab")


class CreateTabOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="ID of the created tab")
    tab_name: str = Field(description="Name of the created tab")


class WriteTabInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="Tab ID to write to")
    content: str = Field(description="Markdown content to write (replaces tab content)")


class WriteTabOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="Tab that was written to")
    characters_written: int = Field(description="Number of characters written")


class ReadTabInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str | None = Field(
        default=None, description="Tab ID to read (None = first/default tab)"
    )


class ReadTabOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tab_id: str = Field(description="Tab that was read")
    content: str = Field(description="Tab content as plain text")


class InsertCommentInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
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
    doc_id: str = Field(description="Google Doc ID")
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
    doc_id: str = Field(description="Google Doc ID")
    content: str = Field(
        description="Markdown content for the Overview tab (replaces existing)"
    )


class UpdateOverviewOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    status: str = Field(description="Update status")


class ListTabsInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")


class TabInfo(BaseModel):
    tab_id: str = Field(description="Tab ID")
    title: str = Field(description="Tab title")


class ListTabsOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    tabs: list[TabInfo] = Field(description="All tabs in the doc")


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


def find_tab_by_id(doc: object, tab_id: str | None) -> tuple[str, object]:
    """Find a tab in a document by ID. Returns (tab_id, tab_dict)."""
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

    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties", {})
        if isinstance(props, dict) and props.get("tabId") == tab_id:
            return (tab_id, tab)

    raise ToolError(f"Tab '{tab_id}' not found in document")


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
    svc = services()
    docs = svc.docs_service()
    result = docs.documents().create(body={"title": params.title}).execute()
    doc_id = result["documentId"]

    if params.share_with:
        drive = svc.drive_service()
        drive.permissions().create(
            fileId=doc_id,
            body={
                "type": "user",
                "role": "writer",
                "emailAddress": params.share_with,
            },
            sendNotificationEmail=False,
        ).execute()

    return CreateDocOutput(
        doc_id=doc_id,
        url=f"https://docs.google.com/document/d/{doc_id}/edit",
    )


@lup_tool(
    "Create a new tab in an existing Google Doc. Use this to create separate "
    "writing spaces for each section (e.g., '§1 Introduction', '§2 Background'). "
    "Each section writer works in its own tab to avoid conflicts during "
    "parallel writing."
)
async def create_tab(params: CreateTabInput) -> CreateTabOutput:
    svc = services()
    docs = svc.docs_service()

    requests = [
        {
            "addTab": {
                "tabProperties": {
                    "title": params.tab_name,
                }
            }
        }
    ]
    result = (
        docs.documents()
        .batchUpdate(
            documentId=params.doc_id,
            body={"requests": requests},
        )
        .execute()
    )

    replies = result.get("replies", [])
    tab_id = ""
    if replies:
        first_reply = replies[0]
        if isinstance(first_reply, dict):
            add_tab = first_reply.get("addTab", {})
            if isinstance(add_tab, dict):
                props = add_tab.get("tabProperties", {})
                if isinstance(props, dict):
                    tab_id = str(props.get("tabId", ""))

    return CreateTabOutput(
        doc_id=params.doc_id,
        tab_id=tab_id,
        tab_name=params.tab_name,
    )


@lup_tool(
    "Write markdown content to a specific tab in the Google Doc. Replaces "
    "the entire tab content. The markdown is converted to native Google Docs "
    "formatting (headings, bold, italic, links, lists). For incremental "
    "updates, read first, modify, then write back."
)
async def write_tab(params: WriteTabInput) -> WriteTabOutput:
    svc = services()
    docs = svc.docs_service()

    doc = (
        docs.documents()
        .get(
            documentId=params.doc_id,
            includeTabsContent=True,
        )
        .execute()
    )

    _, tab = find_tab_by_id(doc, params.tab_id)
    end_index = get_tab_end_index(tab)

    requests: list[dict[str, object]] = []

    if end_index > 1:
        requests.append(clear_tab_request(end_index, tab_id=params.tab_id))

    content_requests = markdown_to_requests(params.content, tab_id=params.tab_id)
    requests.extend(content_requests)

    if requests:
        docs.documents().batchUpdate(
            documentId=params.doc_id,
            body={"requests": requests},
        ).execute()

    return WriteTabOutput(
        doc_id=params.doc_id,
        tab_id=params.tab_id,
        characters_written=len(params.content),
    )


@lup_tool(
    "Read the content of a tab in the Google Doc. Use this to read section "
    "drafts before merging, to check what's been written, or to read the "
    "current state of any tab. Returns plain text content."
)
async def read_tab(params: ReadTabInput) -> ReadTabOutput:
    svc = services()
    docs = svc.docs_service()

    doc = (
        docs.documents()
        .get(
            documentId=params.doc_id,
            includeTabsContent=True,
        )
        .execute()
    )

    tab_id, tab = find_tab_by_id(doc, params.tab_id)
    content = extract_tab_text(tab)

    return ReadTabOutput(
        doc_id=params.doc_id,
        tab_id=tab_id,
        content=content,
    )


@lup_tool(
    "Insert a comment on the Google Doc. Use this for questions to the author "
    "(e.g., 'Should this use the technical definition?'), for reviewer findings "
    "anchored to specific text, and for any async communication. The author "
    "sees comments in real time and can reply whenever convenient. "
    "Anchor to specific text by providing anchor_text."
)
async def insert_comment(params: InsertCommentInput) -> InsertCommentOutput:
    svc = services()
    drive = svc.drive_service()

    body: dict[str, object] = {"content": params.content}

    if params.anchor_text:
        body["anchor"] = params.anchor_text
        body["quotedFileContent"] = {
            "mimeType": "text/plain",
            "value": params.anchor_text,
        }

    result = (
        drive.comments()
        .create(
            fileId=params.doc_id,
            body=body,
            fields="commentId,content",
        )
        .execute()
    )

    return InsertCommentOutput(
        doc_id=params.doc_id,
        comment_id=result.get("commentId", ""),
        content=params.content,
    )


@lup_tool(
    "Read all comments on the Google Doc, including replies. Use this before "
    "the final rewrite to incorporate author feedback, or during any revision "
    "pass. Returns comment text, anchor text, replies, and resolved status."
)
async def read_comments(params: ReadCommentsInput) -> ReadCommentsOutput:
    svc = services()
    drive = svc.drive_service()

    comments_list: list[CommentEntry] = []
    page_token: str | None = None

    while True:
        kwargs: dict[str, object] = {
            "fileId": params.doc_id,
            "fields": "comments(commentId,content,author/displayName,quotedFileContent/value,replies/content,resolved),nextPageToken",
            "pageSize": 100,
        }
        if not params.include_resolved:
            kwargs["fields"] = (
                "comments(commentId,content,author/displayName,quotedFileContent/value,replies/content,resolved),nextPageToken"
            )
        if page_token:
            kwargs["pageToken"] = page_token

        result = drive.comments().list(**kwargs).execute()
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
                    comment_id=str(item.get("commentId", "")),
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
        doc_id=params.doc_id,
        comments=comments_list,
    )


@lup_tool(
    "Update the Overview tab of the Google Doc with current progress. Use this "
    "to keep the author informed about pipeline status: which sections are "
    "being written, which are done, what questions are pending. The Overview "
    "tab is the author's dashboard for the writing session."
)
async def update_overview(params: UpdateOverviewInput) -> UpdateOverviewOutput:
    svc = services()
    docs = svc.docs_service()

    doc = (
        docs.documents()
        .get(
            documentId=params.doc_id,
            includeTabsContent=True,
        )
        .execute()
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

                    requests: list[dict[str, object]] = []
                    if end_index > 1:
                        requests.append(
                            clear_tab_request(end_index, tab_id=overview_tab_id)
                        )
                    requests.extend(
                        markdown_to_requests(params.content, tab_id=overview_tab_id)
                    )
                    if requests:
                        docs.documents().batchUpdate(
                            documentId=params.doc_id,
                            body={"requests": requests},
                        ).execute()
                    break

    if overview_tab_id is None:
        raise ToolError("No Overview tab found. Create one first with create_tab.")

    return UpdateOverviewOutput(
        doc_id=params.doc_id,
        status="updated",
    )


@lup_tool(
    "List all tabs in a Google Doc. Use this to discover available section "
    "tabs, check what's been created, or find the tab IDs needed for "
    "read/write operations."
)
async def list_tabs(params: ListTabsInput) -> ListTabsOutput:
    svc = services()
    docs = svc.docs_service()

    doc = docs.documents().get(documentId=params.doc_id).execute()
    tabs_raw = doc.get("tabs", [])

    tab_list: list[TabInfo] = []
    if isinstance(tabs_raw, list):
        for tab in tabs_raw:
            if not isinstance(tab, dict):
                continue
            props = tab.get("tabProperties", {})
            if isinstance(props, dict):
                tab_list.append(
                    TabInfo(
                        tab_id=str(props.get("tabId", "")),
                        title=str(props.get("title", "")),
                    )
                )

    return ListTabsOutput(
        doc_id=params.doc_id,
        tabs=tab_list,
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
]
