"""The feedback sub-app: the library's loop commands, and inkwell's intake view.

``feedback status`` is the library's self-improvement report — how much session
data the loop holds and what has been analyzed. What only inkwell has is the
other sense of the word: a run reads the author's comments off a Google Doc,
and ``feedback ingest-status`` reports what that intake took in against what is
still pending.

It reads the session state the pipeline writes — the note files, the ledgers,
the recorded outcome of each channel's last poll — rather than polling Drive
for a second view of the truth, because the question it answers is what *this
run* took in, and a fresh fetch would answer a different one.
"""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, Field, ValidationError

from lup.devtools.feedback.app import create_feedback_app
from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.utils import JSON_OPT, format_table, output_json
from lup.workspace.history import iter_session_dirs

from inkwell.agent.core import pipeline_notes_dir
from inkwell.agent.models import ClassifiedComment, IntakeRecord, PipelineSnapshot
from inkwell.agent.session import CommentRecords

logger = logging.getLogger(__name__)

RECORDED_DIRS = ("comments", "terminal", "processed")
"""The notes directories a recorded comment can be filed under.

``comments`` and ``terminal`` are the live feedback set; ``processed`` holds
what has been acted on or dismissed. A comment in any of them is accounted
for, which is what separates an intake that worked from one that lost a note.
"""


class RecordedNote(BaseModel):
    """One comment a run wrote down, and where it filed it."""

    comment_id: str = Field(description="Drive comment id, or a channel-local one")
    impact: str = Field(description="How the classifier ranked it")
    where: str = Field(description="Which notes directory holds it")


class NotesOnDisk(BaseModel):
    """What reading a session's note files found, including what would not parse.

    An unreadable note is neither ingested nor pending, so a reader that
    skipped it quietly would report the comment it holds as lost with no way
    to tell that the file is right there.
    """

    recorded: list[RecordedNote] = Field(
        default_factory=list, description="Notes that still parse"
    )
    unreadable: list[Path] = Field(
        default_factory=list, description="Note files that no longer parse"
    )


class IngestStatus(BaseModel):
    """What a session's author feedback intake took in, against what is pending."""

    session_id: str = Field(description="The session this reports on")
    notes_dir: Path = Field(description="Where the pipeline wrote its notes")
    recorded: list[RecordedNote] = Field(
        default_factory=list, description="Comments with a note file on disk"
    )
    unreadable: list[Path] = Field(
        default_factory=list, description="Note files that no longer parse"
    )
    unrecorded_seen: list[str] = Field(
        default_factory=list,
        description="Ids accounted for with no note file to show for them",
    )
    pending: list[str] = Field(
        default_factory=list,
        description="Unresolved comments the last poll saw and nothing recorded",
    )
    outcomes: list[IntakeRecord] = Field(
        default_factory=list, description="How each channel's last poll went"
    )

    @property
    def unreachable(self) -> list[IntakeRecord]:
        """The channels whose last poll never reached Drive."""
        return [record for record in self.outcomes if record.unreachable]


class NoteFile(BaseModel):
    """One note file on disk, and the directory it was filed in."""

    path: Path = Field(description="Where the file is")
    where: str = Field(description="Which notes directory holds it")


def note_files(base: Path) -> Iterator[NoteFile]:
    """Every note file under the directories a comment can be filed in."""
    for name in RECORDED_DIRS:
        directory = base / name
        if directory.exists():
            yield from (
                NoteFile(path=path, where=name)
                for path in sorted(directory.glob("*.json"))
            )


def read_notes(base: Path) -> NotesOnDisk:
    """Every comment this session wrote down, wherever it filed it."""

    def note(found: NoteFile) -> RecordedNote | None:
        """One file as the report lists it, or nothing if it will not parse."""
        try:
            comment = ClassifiedComment.model_validate_json(
                found.path.read_text(encoding="utf-8")
            )
        except ValidationError:
            logger.exception("Unreadable note file: %s", found.path)
            return None
        return RecordedNote(
            comment_id=comment.comment_id, impact=comment.impact, where=found.where
        )

    files = list(note_files(base))
    read = [note(found) for found in files]
    return NotesOnDisk(
        recorded=[entry for entry in read if entry is not None],
        unreadable=[found.path for found, entry in zip(files, read) if entry is None],
    )


def snapshot_seen(base: Path) -> list[str]:
    """The comment ids the last snapshot recorded as accounted for."""
    path = base / "snapshot.json"
    if not path.exists():
        return []
    try:
        snapshot = PipelineSnapshot.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except ValidationError:
        logger.exception("Unreadable pipeline snapshot: %s", path)
        return []
    return [*snapshot.seen_comment_ids, *snapshot.seen_source_comment_ids]


def accounted_ids(base: Path, records: CommentRecords) -> list[str]:
    """Every comment id this session accounted for, from ledgers and snapshot.

    The append-only ledgers run ahead of the snapshot between stage
    boundaries, and a session that predates them has only the snapshot, so
    both are read and the union is what counts.
    """
    from_ledgers = [
        line.strip()
        for name in ("seen_comments", "seen_source_comments")
        for line in ledger_lines(records.ledger(name))
        if line.strip()
    ]
    return sorted({*from_ledgers, *snapshot_seen(base)})


def ledger_lines(path: Path) -> list[str]:
    """One ledger file's lines, empty where the run never wrote it."""
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def read_status(session_id: str, session_dir: Path) -> IngestStatus:
    """Read one session's intake state off disk."""
    base = pipeline_notes_dir(session_dir)
    records = CommentRecords(base)
    notes = read_notes(base)
    recorded_ids = [note.comment_id for note in notes.recorded]
    accounted = accounted_ids(base, records)
    outcomes = records.outcomes()
    unresolved = sorted(
        {comment_id for record in outcomes for comment_id in record.unresolved}
    )
    return IngestStatus(
        session_id=session_id,
        notes_dir=base,
        recorded=notes.recorded,
        unreadable=notes.unreadable,
        unrecorded_seen=[
            comment_id for comment_id in accounted if comment_id not in recorded_ids
        ],
        pending=[
            comment_id
            for comment_id in unresolved
            if comment_id not in accounted and comment_id not in recorded_ids
        ],
        outcomes=outcomes,
    )


def render(status: IngestStatus) -> str:
    """The status as the terminal shows it."""

    def lines() -> Iterator[str]:
        yield f"Session {status.session_id}"
        yield f"  notes: {status.notes_dir}"
        yield ""
        yield f"Ingested ({len(status.recorded)} note file(s))"
        if status.recorded:
            yield format_table(
                ["COMMENT", "IMPACT", "FILED UNDER"],
                [
                    [note.comment_id or "(unnamed)", note.impact, note.where]
                    for note in status.recorded
                ],
            )
        else:
            yield "  nothing recorded yet"

        yield ""
        yield "Last poll per channel"
        if not status.outcomes:
            yield "  no poll recorded yet"
        for record in status.outcomes:
            if record.unreachable:
                yield (
                    f"  {record.channel}: DRIVE UNREACHABLE — {record.unreachable} "
                    f"(at {record.at})"
                )
            else:
                yield (
                    f"  {record.channel}: read {len(record.unresolved)} unresolved "
                    f"comment(s) at {record.at}"
                )

        yield ""
        for record in status.unreachable:
            yield (
                f"Pending on {record.channel}: unknown — that poll never reached "
                "Drive, so anything left there is still unread"
            )
        if status.pending:
            yield f"Pending ({len(status.pending)} unresolved, not yet ingested)"
            yield from (f"  {comment_id}" for comment_id in status.pending)
        elif status.unreachable:
            yield "Pending: nothing on the channels that answered"
        else:
            yield "Pending: nothing — every unresolved comment the last poll saw"
            yield "  has been ingested"

        if status.unrecorded_seen:
            yield ""
            yield (
                f"Accounted for with no note file ({len(status.unrecorded_seen)}) — "
                "recorded before this run kept notes, or lost:"
            )
            yield from (f"  {comment_id}" for comment_id in status.unrecorded_seen)

        if status.unreadable:
            yield ""
            yield f"Note files that no longer parse ({len(status.unreadable)}):"
            yield from (f"  {path}" for path in status.unreadable)

    return "\n".join(lines())


def latest_session() -> Path | None:
    """The session whose pipeline notes were written most recently."""
    with_notes = [
        directory
        for directory in iter_session_dirs()
        if pipeline_notes_dir(directory).exists()
    ]
    if not with_notes:
        return None
    return max(with_notes, key=lambda d: pipeline_notes_dir(d).stat().st_mtime)


def create_app(prompt: Callable[[], AgentPrompt]) -> typer.Typer:
    """The feedback tree: the library's loop commands, plus inkwell's own."""
    app = create_feedback_app(prompt)

    @app.command("ingest-status")
    def ingest_status_cmd(
        session: Annotated[
            str | None,
            typer.Option("--session", "-s", help="Session id (default: most recent)"),
        ] = None,
        as_json: JSON_OPT = False,
    ) -> None:
        """Report what author feedback a session ingested, and what is pending."""
        session_dir = (
            latest_session()
            if session is None
            else next(iter_session_dirs(session), None)
        )
        if session_dir is None:
            typer.echo(
                f"No session notes found for {session!r}"
                if session
                else "No session has written pipeline notes yet",
                err=True,
            )
            raise typer.Exit(1)

        status = read_status(session or session_dir.name, session_dir)
        if as_json:
            output_json(status)
            return
        typer.echo(render(status))

    return app
