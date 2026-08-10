"""Live session state for the writing pipeline.

Tracks doc_id, section status, pending questions, and author comments.
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false
# Google API service objects are untyped.

import asyncio
import logging
from pathlib import Path
from typing import TypedDict

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
        self.title: str = ""
        self.source_doc_id: str = ""
        self.stage: str = "starting"
        self.sections: list[SectionStatus] = []
        self.pending_questions: list[str] = []
        self.seen_comment_ids: set[str] = set()
        self.agent_comment_ids: set[str] = set()
        self.seen_source_comment_ids: set[str] = set()
        self.sleep_entered: asyncio.Event = asyncio.Event()
        self.directions_tab_id: str = ""
        self.last_directions_content: str = ""
        self.shared_dir: Path | None = None
        self.agent_ids_path: Path | None = None

    def set_doc(self, doc_id: str, doc_url: str) -> None:
        self.doc_id = doc_id
        self.doc_url = doc_url

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    def add_section(self, title: str, tab_id: str, status: str = "planned") -> None:
        for section in self.sections:
            if section["title"] == title:
                section["tab_id"] = tab_id
                return
        self.sections.append(SectionStatus(title=title, tab_id=tab_id, status=status))

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
            from inkwell.agent.tools.google_docs_schema import Comment, CommentPage

            svc = services()
            drive = svc.drive_service()

            page = CommentPage.model_validate(
                await execute_with_retry(
                    drive.comments().list(
                        fileId=self.doc_id,
                        fields="comments(id,replies(id,content),resolved)",
                        pageSize=100,
                    )
                )
            )

            def unread(comment: Comment) -> bool:
                """Whether this comment is something the author is waiting on.

                A comment inkwell did not write is unread on its own; one it
                did is unread only once somebody else has replied under it.
                """
                if comment.id in self.agent_comment_ids:
                    return any(
                        reply.id not in self.agent_comment_ids
                        for reply in comment.replies
                    )
                return True

            return sum(
                1
                for comment in page.comments
                if not comment.resolved
                and comment.id not in self.seen_comment_ids
                and unread(comment)
            )
        except Exception:
            logger.warning("Failed to check unread comments", exc_info=True)
            return 0

    def attach_agent_ids_registry(self, path: Path) -> None:
        """Persist agent-authored comment/reply IDs across process restarts.

        Snapshots can lag behind comment posting; a resumed process that
        trusts only the snapshot will re-ingest the agent's own comments
        as author feedback. The registry file is append-only and survives
        crashes between snapshot saves.
        """
        self.agent_ids_path = path
        if path.exists():
            self.agent_comment_ids.update(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )

    def mark_agent_comment(self, comment_id: str) -> None:
        self.agent_comment_ids.add(comment_id)
        if self.agent_ids_path is not None and comment_id:
            with self.agent_ids_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{comment_id}\n")

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
                    fields="comments(id,content,quotedFileContent/value,replies(id,content),resolved)",
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
                    fields="comments(id,content,quotedFileContent/value,replies(id,content),resolved)",
                    pageSize=100,
                )
                .execute()
            )

            return self.parse_comments(result)
        except Exception:
            logger.warning("Failed to fetch author comments (sync)", exc_info=True)
            return []

    def get_new_source_comments_sync(self) -> list[AuthorComment]:
        """Fetch new comments from the source Google Doc (sync)."""
        if not self.source_doc_id:
            return []

        try:
            from inkwell.agent.tools.google_docs import services

            svc = services()
            drive = svc.drive_service()

            result = (
                drive.comments()
                .list(
                    fileId=self.source_doc_id,
                    fields="comments(id,content,quotedFileContent/value,replies(id,content),resolved)",
                    pageSize=100,
                )
                .execute()
            )

            return self.parse_source_comments(result)
        except Exception:
            logger.warning("Failed to fetch source doc comments (sync)", exc_info=True)
            return []

    def parse_source_comments(self, result: object) -> list[AuthorComment]:
        """Parse source doc comments, tracking seen IDs separately."""
        if not isinstance(result, dict):
            return []

        new_comments: list[AuthorComment] = []
        for item in result.get("comments", []):
            if not isinstance(item, dict):
                continue
            if item.get("resolved", False):
                continue

            comment_id = str(item.get("id", ""))
            if comment_id in self.seen_source_comment_ids:
                continue

            content = str(item.get("content", ""))
            anchor = ""
            quoted = item.get("quotedFileContent")
            if isinstance(quoted, dict):
                anchor = str(quoted.get("value", ""))

            reply_list = item.get("replies", [])
            reply_text = ""
            if isinstance(reply_list, list) and reply_list:
                last_reply = reply_list[-1]
                if isinstance(last_reply, dict):
                    reply_text = str(last_reply.get("content", ""))

            new_comments.append(
                AuthorComment(
                    comment_id=comment_id,
                    content=content,
                    anchor_text=anchor,
                    reply=reply_text,
                )
            )
            self.seen_source_comment_ids.add(comment_id)

        return new_comments

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
            author_replies = (
                [
                    r
                    for r in reply_list
                    if isinstance(r, dict)
                    and str(r.get("id", "")) not in self.agent_comment_ids
                ]
                if isinstance(reply_list, list)
                else []
            )

            if author_replies:
                reply_text = str(author_replies[-1].get("content", ""))
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
