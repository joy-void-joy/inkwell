"""Live session state for the writing pipeline.

Tracks doc_id, section status, pending questions, and author comments.
"""

import asyncio
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from pydantic import BaseModel, Field, ValidationError

from lup.types import JsonValue

if TYPE_CHECKING:
    from inkwell.agent.tools.google_docs_schema import CommentPage

logger = logging.getLogger(__name__)


class SectionStatus(TypedDict):
    title: str
    tab_id: str
    status: str


class CommentLedger(BaseModel):
    """The comment ids a session has already accounted for.

    Membership is the only question ever asked of it — has this comment been
    surfaced to the author, did the agent write it — so it answers that
    instead of handing out the collection it keeps. The order it keeps them
    in is the order the append-only registry file on disk records them.
    """

    handled: list[str] = Field(
        default_factory=list, description="Ids accounted for, in the order seen"
    )

    def __contains__(self, comment_id: str) -> bool:
        """Whether `comment_id` has already been accounted for."""
        return comment_id in self.handled

    def mark(self, comment_id: str) -> None:
        """Account for `comment_id`, unless it already is."""
        if comment_id not in self.handled:
            self.handled.append(comment_id)

    def mark_all(self, comment_ids: Iterable[str]) -> None:
        """Account for every id in `comment_ids`."""
        for comment_id in comment_ids:
            self.mark(comment_id)


class AuthorComment(TypedDict):
    comment_id: str
    content: str
    anchor_text: str
    reply: str


def comment_page(result: JsonValue) -> "CommentPage":
    """The comments a Drive response carries, empty when it carries none.

    A page that does not validate is logged and read as empty rather than
    raised: a polling loop should skip a malformed response, not die on it.
    """
    from inkwell.agent.tools.google_docs_schema import CommentPage

    try:
        return CommentPage.model_validate(result)
    except ValidationError:
        logger.warning("Unreadable Drive comments payload", exc_info=True)
        return CommentPage()


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
        self.seen_comments = CommentLedger()
        self.agent_comments = CommentLedger()
        self.seen_source_comments = CommentLedger()
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
                if comment.id in self.agent_comments:
                    return any(
                        reply.id not in self.agent_comments for reply in comment.replies
                    )
                return True

            return sum(
                1
                for comment in page.comments
                if not comment.resolved
                and comment.id not in self.seen_comments
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
            self.agent_comments.mark_all(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )

    def mark_agent_comment(self, comment_id: str) -> None:
        self.agent_comments.mark(comment_id)
        if self.agent_ids_path is not None and comment_id:
            with self.agent_ids_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{comment_id}\n")

    def mark_comments_seen(self, comment_ids: list[str]) -> None:
        self.seen_comments.mark_all(comment_ids)

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

    def parse_source_comments(self, result: JsonValue) -> list[AuthorComment]:
        """Read source-doc comments, tracking their seen ids separately."""

        def fresh() -> Iterator[AuthorComment]:
            for comment in comment_page(result).comments:
                if comment.resolved or comment.id in self.seen_source_comments:
                    continue
                self.seen_source_comments.mark(comment.id)
                yield AuthorComment(
                    comment_id=comment.id,
                    content=comment.content,
                    anchor_text=comment.anchor_text(),
                    reply=comment.replies[-1].content if comment.replies else "",
                )

        return list(fresh())

    def parse_comments(self, result: JsonValue) -> list[AuthorComment]:
        """Read the comments API response, marking each comment it yields seen.

        A comment the agent posted is only news once somebody else has replied
        under it; one the author opened is news on its own.
        """

        def fresh() -> Iterator[AuthorComment]:
            for comment in comment_page(result).comments:
                if comment.resolved or comment.id in self.seen_comments:
                    continue
                author_replies = [
                    reply
                    for reply in comment.replies
                    if reply.id not in self.agent_comments
                ]
                if not author_replies and comment.id in self.agent_comments:
                    continue
                self.seen_comments.mark(comment.id)
                yield AuthorComment(
                    comment_id=comment.id,
                    content=comment.content,
                    anchor_text=comment.anchor_text(),
                    reply=author_replies[-1].content if author_replies else "",
                )

        return list(fresh())
