"""Live session state for the writing pipeline.

Tracks doc_id, section status, pending questions, and author comments.

Author feedback arrives here first: every document inkwell watches is polled
through one implementation that reads all of Drive's pages, hands back a result
that says whether it reached Drive at all, and accounts for nothing — a comment
is marked seen by whoever wrote it down, so nothing is lost between the two.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.agent.models import IntakeRecord
from inkwell.agent.tools.google_docs_schema import Comment

logger = logging.getLogger(__name__)


class SectionStatus(TypedDict):
    title: str
    tab_id: str
    status: str


class CommentLedger(BaseModel):
    """What a session has already accounted for, by id or by key.

    Membership is the only question ever asked of it — has this comment been
    surfaced to the author, did the agent write it, has this unresolved
    reference already been raised — so it answers that instead of handing out
    the collection it keeps. The order it keeps them in is the order the
    append-only registry file on disk records them.

    An id is *claimed* from the moment a poll hands it to a caller until that
    caller has written it down, and *handled* once the record is on disk. A
    claim keeps a second poller off a comment already in flight, and lives
    only in memory: a process that dies mid-record leaves the comment
    unhandled, so the next poll offers it again instead of losing it.
    """

    name: str = Field(description="What this ledger's registry file is called")
    handled: list[str] = Field(
        default_factory=list, description="Ids accounted for, in the order seen"
    )
    claimed: list[str] = Field(
        default_factory=list,
        description="Ids handed out and not yet written down, this process only",
    )
    registry: Path | None = Field(
        default=None, description="Append-only file the handled ids survive in"
    )

    def __contains__(self, comment_id: str) -> bool:
        """Whether `comment_id` is accounted for, or on its way to being."""
        return comment_id in self.handled or comment_id in self.claimed

    def attach(self, path: Path) -> None:
        """Back this ledger with an append-only file, taking in what it holds.

        The snapshot is written at stage boundaries, so a process that died
        between two of them would forget what it had accounted for: the
        agent's own comments come back as author feedback, and a comment
        already recorded is recorded twice. Loading runs before the file is
        attached, so reading it does not rewrite it.
        """
        if self.registry == path:
            return
        if path.exists():
            self.mark_all(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        self.registry = path

    def mark(self, comment_id: str) -> None:
        """Account for `comment_id` durably, unless it already is."""
        self.release(comment_id)
        if comment_id in self.handled:
            return
        self.handled.append(comment_id)
        if self.registry is not None and comment_id:
            with self.registry.open("a", encoding="utf-8") as fh:
                fh.write(f"{comment_id}\n")

    def mark_all(self, comment_ids: Iterable[str]) -> None:
        """Account for every id in `comment_ids`."""
        for comment_id in comment_ids:
            self.mark(comment_id)

    def claim(self, comment_ids: Iterable[str]) -> None:
        """Hold these ids while whoever took them writes them down."""
        self.claimed.extend(
            comment_id for comment_id in comment_ids if comment_id not in self.claimed
        )

    def release(self, comment_id: str) -> None:
        """Drop a claim, putting the comment back in front of the next poll."""
        self.claimed = [held for held in self.claimed if held != comment_id]

    @contextmanager
    def recording(self, comment_id: str) -> Iterator[None]:
        """Account for a comment once the block writing it down has finished.

        The order is the whole point: a comment marked seen before its note
        file exists is lost outright if anything in between fails, so the mark
        waits on the write and a failure releases the claim instead.
        """
        try:
            yield
        except Exception:
            self.release(comment_id)
            raise
        else:
            self.mark(comment_id)


class AuthorComment(TypedDict):
    comment_id: str
    content: str
    anchor_text: str
    reply: str


type NewsFilter = Callable[[Comment], AuthorComment | None]
"""Reads one comment as author feedback, or declines it as not news."""


class CommentIntake(BaseModel):
    """What one poll of a document's comments came back with.

    "Drive answered and the author is quiet" and "Drive could not be read" are
    different events with different consequences — under the second, feedback
    may be sitting unread while the run proceeds as though there were none — so
    which one happened travels in the value rather than in a log line no
    caller can branch on.
    """

    model_config = ConfigDict(frozen=True)

    comments: list[AuthorComment] = Field(
        default_factory=list, description="New comments the caller has yet to record"
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Every unresolved comment id the poll read as author feedback",
    )
    unreachable: str = Field(
        default="", description="Why Drive could not be read, empty when it was"
    )

    @property
    def reached(self) -> bool:
        """Whether Drive answered at all."""
        return not self.unreachable

    def record(self, channel: str, doc_id: str) -> IntakeRecord:
        """This poll as the run's durable record of how intake went."""
        return IntakeRecord(
            channel=channel,
            doc_id=doc_id,
            at=datetime.now().isoformat(),
            unreachable=self.unreachable,
            unresolved=list(self.unresolved),
            offered=[comment["comment_id"] for comment in self.comments],
        )


class CommentRecords:
    """Where a session's comment intake lives on disk.

    The ledgers and each channel's last outcome sit beside the notes rather
    than inside the pipeline snapshot, which is written at stage boundaries
    while comments arrive whenever the author writes one. One class owns the
    layout so the run that writes it and the report that reads it cannot come
    to disagree about where it is.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.intake_dir = directory / "intake"

    def ledger(self, name: str) -> Path:
        """The append-only file one ledger's accounted-for ids survive in."""
        return self.directory / f"{name}.txt"

    def save(self, record: IntakeRecord) -> None:
        """Write down how one channel's last poll went."""
        self.intake_dir.mkdir(parents=True, exist_ok=True)
        path = self.intake_dir / f"{record.channel}.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def outcomes(self) -> list[IntakeRecord]:
        """The last poll of every channel, in channel order.

        A record that no longer parses is reported as missing rather than
        raised on: this is what a reader consults when intake looks wrong, and
        it has to survive one unreadable file to say so.
        """
        if not self.intake_dir.exists():
            return []

        def readable() -> Iterator[IntakeRecord]:
            for path in sorted(self.intake_dir.glob("*.json")):
                try:
                    yield IntakeRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except ValidationError:
                    logger.exception("Unreadable intake record: %s", path)

        return list(readable())


class WritingSessionState:
    """Mutable state for a writing session.

    Updated by the agent's tool calls (create_doc, create_tab, etc.) and read
    by the pipeline, the watchers, and whatever surface is showing the author
    their run.
    """

    def __init__(self) -> None:
        self.doc_id: str = ""
        self.doc_url: str = ""
        self.title: str = ""
        self.book: str = ""
        self.source_doc_id: str = ""
        self.stage: str = "starting"
        self.sections: list[SectionStatus] = []
        self.pending_questions: list[str] = []
        self.seen_comments = CommentLedger(name="seen_comments")
        self.agent_comments = CommentLedger(name="agent_ids")
        self.seen_source_comments = CommentLedger(name="seen_source_comments")
        self.raised_references = CommentLedger(name="raised_references")
        self.sleep_entered: asyncio.Event = asyncio.Event()
        self.directions_tab_id: str = ""
        self.last_directions_content: str = ""
        self.shared_dir: Path | None = None
        self.records: CommentRecords | None = None

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

    def attach_records(self, directory: Path) -> None:
        """Persist comment intake under `directory`, taking in what is there.

        Snapshots can lag behind comment posting; a resumed process that
        trusts only the snapshot will re-ingest the agent's own comments as
        author feedback, and re-classify comments it already recorded. Each
        ledger's file is append-only and survives a crash between saves.
        """
        records = CommentRecords(directory)
        for ledger in (
            self.agent_comments,
            self.seen_comments,
            self.seen_source_comments,
            self.raised_references,
        ):
            ledger.attach(records.ledger(ledger.name))
        self.records = records

    def mark_agent_comment(self, comment_id: str) -> None:
        self.agent_comments.mark(comment_id)

    def mark_comments_seen(self, comment_ids: list[str]) -> None:
        self.seen_comments.mark_all(comment_ids)

    def author_news(self, comment: Comment) -> AuthorComment | None:
        """This comment as author feedback, or nothing where it is not news.

        A comment the agent posted is only news once somebody else has replied
        under it; one the author opened is news on its own.
        """
        author_replies = [
            reply for reply in comment.replies if reply.id not in self.agent_comments
        ]
        if not author_replies and comment.id in self.agent_comments:
            return None
        return AuthorComment(
            comment_id=comment.id,
            content=comment.content,
            anchor_text=comment.anchor_text(),
            reply=author_replies[-1].content if author_replies else "",
        )

    def source_news(self, comment: Comment) -> AuthorComment | None:
        """Every unresolved source-doc comment is news: the agent writes none there."""
        return AuthorComment(
            comment_id=comment.id,
            content=comment.content,
            anchor_text=comment.anchor_text(),
            reply=comment.replies[-1].content if comment.replies else "",
        )

    async def poll_comments(
        self,
        channel: str,
        doc_id: str,
        ledger: CommentLedger,
        news: NewsFilter,
        *,
        probe: bool = False,
    ) -> CommentIntake:
        """Read every comment on one document and pick out what is new.

        The one intake path. Which document, which ledger remembers it, and
        what counts as news are the three things that differ between the
        output doc, the source doc, and the unread probe, so they are
        parameters rather than three copies of a poll that drift apart.
        Nothing is accounted for here — the caller marks a comment seen once
        its note is on disk — and the claim a poll takes out keeps a second
        poller off a comment already in flight.

        A ``probe`` only looks: it claims nothing and records no outcome, so it
        can neither take a comment out of the next real poll's way nor
        overwrite what that poll last reported.
        """
        if not doc_id:
            return CommentIntake()

        from inkwell.agent.tools.google_docs import (
            COMMENT_READ_FAILURES,
            fetch_all_comments,
        )

        try:
            comments = await fetch_all_comments(doc_id)
        except COMMENT_READ_FAILURES as exc:
            logger.exception("Could not read comments on document %s", doc_id)
            intake = CommentIntake(unreachable=f"{type(exc).__name__}: {exc}")
        else:
            feedback = [
                author
                for comment in comments
                if not comment.resolved and (author := news(comment)) is not None
            ]
            fresh = [
                author for author in feedback if author["comment_id"] not in ledger
            ]
            if not probe:
                ledger.claim(author["comment_id"] for author in fresh)
            intake = CommentIntake(
                comments=fresh,
                unresolved=[author["comment_id"] for author in feedback],
            )

        if self.records is not None and not probe:
            self.records.save(intake.record(channel, doc_id))
        return intake

    async def poll_author_comments(self) -> CommentIntake:
        """New comments on the output document, or why it could not be read."""
        return await self.poll_comments(
            "author", self.doc_id, self.seen_comments, self.author_news
        )

    async def poll_source_comments(self) -> CommentIntake:
        """New comments on the author's own source document."""
        return await self.poll_comments(
            "source", self.source_doc_id, self.seen_source_comments, self.source_news
        )

    async def probe_author_comments(self) -> CommentIntake:
        """What an ingest would take next, claiming none of it.

        A probe only asks whether the author is waiting on anything, so it
        leaves the comments in front of the poll that will record them, and
        leaves that poll's recorded outcome alone. Its answer still
        distinguishes a quiet document from an unreadable one: a caller that
        reads an unreachable Drive as "nothing unread" tells the author their
        comment was seen and ignored.
        """
        return await self.poll_comments(
            "author",
            self.doc_id,
            self.seen_comments,
            self.author_news,
            probe=True,
        )
