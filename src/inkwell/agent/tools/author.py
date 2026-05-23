"""Author interaction tools — non-blocking communication via Google Doc comments.

These provide semantic wrappers around the Google Docs API for author communication:
- ask_author: leave a tagged question (agent continues with best guess)
- check_author_feedback: read unresolved comments where the author replied
- update_progress: structured progress update to the Overview tab
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false
# Google API service objects are untyped (DocsService/DriveService = object).

import logging
from typing import TypedDict

from pydantic import BaseModel, Field

from inkwell.agent.markdown_to_docs import clear_tab_request, markdown_to_requests
from inkwell.agent.tools.google_docs import (
    find_tab_by_id,
    get_tab_end_index,
    services,
)
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)


class AskAuthorInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    question: str = Field(description="The question to ask the author")
    question_type: str = Field(
        description="Type tag: 'question', 'direction_check', or 'fact_verify'"
    )
    anchor_text: str | None = Field(
        default=None,
        description="Text in the doc to anchor the question to",
    )
    best_guess: str = Field(
        description="Your best guess answer — what you'll proceed with if the author doesn't respond"
    )


class AskAuthorOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    comment_id: str = Field(description="ID of the created comment")
    question_type: str = Field(description="Type tag of the question")


class AuthorReply(TypedDict):
    comment_id: str
    original_question: str
    anchor_text: str
    author_reply: str


class CheckFeedbackInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")


class CheckFeedbackOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    replies: list[AuthorReply] = Field(
        description="Comments where the author has replied"
    )
    unanswered_count: int = Field(
        description="Number of agent questions still unanswered"
    )


class SectionStatus(TypedDict):
    title: str
    status: str


class UpdateProgressInput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    stage: str = Field(
        description="Current pipeline stage (e.g., 'researching', 'writing', 'reviewing')"
    )
    sections: list[SectionStatus] = Field(
        description="Status of each section: title and status ('planned'|'drafted'|'reviewed'|'final')"
    )
    pending_questions: list[str] = Field(
        default_factory=list,
        description="Unanswered questions for the author",
    )
    next_steps: str = Field(description="What the agent will do next")


class UpdateProgressOutput(BaseModel):
    doc_id: str = Field(description="Google Doc ID")
    status: str = Field(description="Update status")


@lup_tool(
    "Ask the author a question via a Google Doc comment. The question is "
    "tagged with a type (question, direction_check, fact_verify) and "
    "includes your best guess — what you'll proceed with if the author "
    "doesn't respond. This is fully non-blocking: leave the question and "
    "continue working. Check for replies later with check_author_feedback. "
    "Use this when you need author input but don't want to block the pipeline."
)
async def ask_author(params: AskAuthorInput) -> AskAuthorOutput:
    svc = services()
    drive = svc.drive_service()

    tag = params.question_type.upper().replace("_", " ")
    comment_text = (
        f"[{tag}] {params.question}\n\n"
        f"My best guess: {params.best_guess}\n\n"
        f"(Reply to override, or I'll proceed with my guess.)"
    )

    body: dict[str, object] = {"content": comment_text}

    if params.anchor_text:
        body["quotedFileContent"] = {
            "mimeType": "text/plain",
            "value": params.anchor_text,
        }

    result = (
        drive.comments()
        .create(
            fileId=params.doc_id,
            body=body,
            fields="commentId",
        )
        .execute()
    )

    comment_id = str(result.get("commentId", ""))

    from inkwell.agent.tools.google_docs import SESSION_STATE

    if SESSION_STATE is not None:
        SESSION_STATE.add_question(params.question)
        SESSION_STATE.mark_agent_comment(comment_id)

    return AskAuthorOutput(
        doc_id=params.doc_id,
        comment_id=comment_id,
        question_type=params.question_type,
    )


@lup_tool(
    "Check for author feedback on the Google Doc. Reads all unresolved "
    "comments and returns those where the author has replied. Use this "
    "at pipeline checkpoints (before merging, before final rewrite) to "
    "incorporate author direction. Returns the original question, the "
    "author's reply, and the anchor text for context."
)
async def check_author_feedback(
    params: CheckFeedbackInput,
) -> CheckFeedbackOutput:
    svc = services()
    drive = svc.drive_service()

    result = (
        drive.comments()
        .list(
            fileId=params.doc_id,
            fields="comments(commentId,content,quotedFileContent/value,replies/content,resolved)",
            pageSize=100,
        )
        .execute()
    )

    replies: list[AuthorReply] = []
    unanswered = 0
    seen_ids: list[str] = []

    for item in result.get("comments", []):
        if not isinstance(item, dict):
            continue
        if item.get("resolved", False):
            continue

        comment_id = str(item.get("commentId", ""))
        content = str(item.get("content", ""))
        anchor = ""
        quoted = item.get("quotedFileContent")
        if isinstance(quoted, dict):
            anchor = str(quoted.get("value", ""))

        reply_list = item.get("replies", [])
        if isinstance(reply_list, list) and reply_list:
            last_reply = reply_list[-1]
            if isinstance(last_reply, dict):
                reply_text = str(last_reply.get("content", ""))
                replies.append(
                    AuthorReply(
                        comment_id=comment_id,
                        original_question=content,
                        anchor_text=anchor,
                        author_reply=reply_text,
                    )
                )
                seen_ids.append(comment_id)
        elif content.startswith("["):
            unanswered += 1

    from inkwell.agent.tools.google_docs import SESSION_STATE

    if SESSION_STATE is not None and seen_ids:
        SESSION_STATE.mark_comments_seen(seen_ids)

    return CheckFeedbackOutput(
        doc_id=params.doc_id,
        replies=replies,
        unanswered_count=unanswered,
    )


@lup_tool(
    "Update the Overview tab with a structured progress dashboard. Use this "
    "to keep the author informed about the pipeline stage, section status, "
    "pending questions, and next steps. Call this before sleeping so the "
    "author has a clear picture of where things stand."
)
async def update_progress(params: UpdateProgressInput) -> UpdateProgressOutput:
    svc = services()
    docs = svc.docs_service()

    status_markers = {
        "planned": "[ ]",
        "writing": "[>]",
        "drafted": "[~]",
        "reviewed": "[~]",
        "final": "[x]",
    }

    lines = [
        "# Writing Progress",
        "",
        f"**Stage:** {params.stage}",
        "",
        "## Sections",
        "",
    ]

    for section in params.sections:
        marker = status_markers.get(section["status"], "[ ]")
        lines.append(f"- {marker} **{section['title']}** — {section['status']}")

    if params.pending_questions:
        lines.append("")
        lines.append("## Pending Questions")
        lines.append("")
        for q in params.pending_questions:
            lines.append(f"- {q}")

    lines.append("")
    lines.append(f"**Next:** {params.next_steps}")

    from inkwell.agent.tools.google_docs import SESSION_STATE

    if SESSION_STATE is not None:
        SESSION_STATE.set_stage(params.stage)
        for section in params.sections:
            SESSION_STATE.update_section_status(section["title"], section["status"])

    content = "\n".join(lines)

    doc = (
        docs.documents()
        .get(
            documentId=params.doc_id,
            includeTabsContent=True,
        )
        .execute()
    )

    tabs = doc.get("tabs", [])
    if not isinstance(tabs, list):
        raise ToolError("Could not read document tabs")

    overview_tab_id: str | None = None
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties", {})
        if isinstance(props, dict):
            title = props.get("title", "")
            if isinstance(title, str) and title.lower() in ("overview", ""):
                overview_tab_id = str(props.get("tabId", ""))
                break

    if overview_tab_id is None:
        raise ToolError("No Overview tab found. Create one first with create_tab.")

    _, matched_tab = find_tab_by_id(doc, overview_tab_id)
    end_index = get_tab_end_index(matched_tab)

    requests: list[dict[str, object]] = []
    if end_index > 1:
        requests.append(clear_tab_request(end_index, tab_id=overview_tab_id))
    requests.extend(markdown_to_requests(content, tab_id=overview_tab_id))

    if requests:
        docs.documents().batchUpdate(
            documentId=params.doc_id,
            body={"requests": requests},
        ).execute()

    return UpdateProgressOutput(
        doc_id=params.doc_id,
        status="updated",
    )


AUTHOR_TOOLS = [ask_author, check_author_feedback, update_progress]
