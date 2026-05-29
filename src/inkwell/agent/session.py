"""Live session state for the writing pipeline.

Tracks doc_id, section status, pending questions, and author comments.
Provides check_unread() for the pending-event guard and build_context()
for the realtime context tool.
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false
# Google API service objects are untyped.

import asyncio
import logging
from typing import TypedDict

from pydantic import Field

from inkwell.agent.tools.realtime import ContextOutput

logger = logging.getLogger(__name__)


class SectionStatus(TypedDict):
    title: str
    tab_id: str
    status: str


class AuthorComment(TypedDict):
    comment_id: str
    content: str
    anchor_text: str
    reply: str


class WritingSessionState:
    """Mutable state for a writing session.

    Updated by the agent's tool calls (create_doc, create_tab, etc.)
    and queried by build_context and check_unread.
    """

    def __init__(self) -> None:
        self.doc_id: str = ""
        self.doc_url: str = ""
        self.stage: str = "starting"
        self.sections: list[SectionStatus] = []
        self.pending_questions: list[str] = []
        self.seen_comment_ids: set[str] = set()
        self.agent_comment_ids: set[str] = set()
        self.sleep_entered: asyncio.Event = asyncio.Event()

    def set_doc(self, doc_id: str, doc_url: str) -> None:
        self.doc_id = doc_id
        self.doc_url = doc_url

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    def add_section(self, title: str, tab_id: str) -> None:
        self.sections.append(
            SectionStatus(title=title, tab_id=tab_id, status="planned")
        )

    def update_section_status(self, title: str, status: str) -> None:
        for section in self.sections:
            if section["title"] == title:
                section["status"] = status
                return

    def add_question(self, question: str) -> None:
        self.pending_questions.append(question)

    async def check_unread_comments(self) -> int:
        """Count unread author replies on the Google Doc.

        Returns 0 if no doc is set or if the API call fails.
        """
        if not self.doc_id:
            return 0

        try:
            from inkwell.agent.tools.google_docs import execute_with_retry, services

            svc = services()
            drive = svc.drive_service()

            result = await execute_with_retry(
                drive.comments().list(
                    fileId=self.doc_id,
                    fields="comments(id,replies/content,resolved)",
                    pageSize=100,
                )
            )

            unread = 0
            for item in result.get("comments", []):
                if not isinstance(item, dict):
                    continue
                if item.get("resolved", False):
                    continue
                comment_id = str(item.get("id", ""))
                if comment_id in self.seen_comment_ids:
                    continue

                is_agent_comment = comment_id in self.agent_comment_ids
                reply_list = item.get("replies", [])
                has_replies = isinstance(reply_list, list) and bool(reply_list)

                if has_replies and is_agent_comment:
                    unread += 1
                elif not is_agent_comment:
                    unread += 1

            return unread
        except Exception:
            logger.warning("Failed to check unread comments", exc_info=True)
            return 0

    def mark_agent_comment(self, comment_id: str) -> None:
        self.agent_comment_ids.add(comment_id)

    def mark_comments_seen(self, comment_ids: list[str]) -> None:
        self.seen_comment_ids.update(comment_ids)

    async def get_new_author_comments(self) -> list[AuthorComment]:
        """Fetch author replies not yet seen (async with retry)."""
        if not self.doc_id:
            return []

        try:
            from inkwell.agent.tools.google_docs import execute_with_retry, services

            svc = services()
            drive = svc.drive_service()

            result = await execute_with_retry(
                drive.comments().list(
                    fileId=self.doc_id,
                    fields="comments(id,content,quotedFileContent/value,replies/content,resolved)",
                    pageSize=100,
                )
            )

            return self.parse_comments(result)
        except Exception:
            logger.warning("Failed to fetch author comments", exc_info=True)
            return []

    def get_new_author_comments_sync(self) -> list[AuthorComment]:
        """Synchronous fallback for contexts that can't await (e.g. watcher poll)."""
        if not self.doc_id:
            return []

        try:
            from inkwell.agent.tools.google_docs import services

            svc = services()
            drive = svc.drive_service()

            result = (
                drive.comments()
                .list(
                    fileId=self.doc_id,
                    fields="comments(id,content,quotedFileContent/value,replies/content,resolved)",
                    pageSize=100,
                )
                .execute()
            )

            return self.parse_comments(result)
        except Exception:
            logger.warning("Failed to fetch author comments (sync)", exc_info=True)
            return []

    def parse_comments(self, result: object) -> list[AuthorComment]:
        """Parse comments API response into AuthorComment list, updating seen set."""
        if not isinstance(result, dict):
            return []

        new_comments: list[AuthorComment] = []
        for item in result.get("comments", []):
            if not isinstance(item, dict):
                continue
            if item.get("resolved", False):
                continue

            comment_id = str(item.get("id", ""))
            if comment_id in self.seen_comment_ids:
                continue

            content = str(item.get("content", ""))
            anchor = ""
            quoted = item.get("quotedFileContent")
            if isinstance(quoted, dict):
                anchor = str(quoted.get("value", ""))

            is_agent_comment = comment_id in self.agent_comment_ids
            reply_list = item.get("replies", [])
            has_replies = isinstance(reply_list, list) and bool(reply_list)

            if has_replies:
                last_reply = reply_list[-1]
                if not isinstance(last_reply, dict):
                    continue
                reply_text = str(last_reply.get("content", ""))
            elif not is_agent_comment:
                reply_text = ""
            else:
                continue

            new_comments.append(
                AuthorComment(
                    comment_id=comment_id,
                    content=content,
                    anchor_text=anchor,
                    reply=reply_text,
                )
            )
            self.seen_comment_ids.add(comment_id)

        return new_comments


class WritingContext(ContextOutput):
    """Rich context returned by the context tool during writing sessions."""

    stage: str = Field(description="Current pipeline stage")
    doc_id: str = Field(default="", description="Google Doc ID")
    doc_url: str = Field(default="", description="Google Doc URL")
    sections_status: list[SectionStatus] = Field(
        default_factory=list, description="Status of each section"
    )
    pending_questions: list[str] = Field(
        default_factory=list, description="Unanswered questions for the author"
    )
    new_author_comments: list[AuthorComment] = Field(
        default_factory=list, description="Author replies since last check"
    )
    scheduler: dict[str, object] = Field(
        default_factory=dict, description="Scheduler timing state"
    )
