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
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, Field, ValidationError

from lup.devtools.feedback.app import create_feedback_app
from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.utils import JSON_OPT, format_table, output_json
from lup.workspace.history import iter_session_dirs

from inkwell.agent.core import pipeline_notes_dir
from inkwell.agent.models import (
    ClassifiedComment,
    IntakeRecord,
    PipelineSnapshot,
    ReviewFinding,
    RewriteDispositions,
)
from inkwell.agent.pipeline import finding_tag
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


class ReviewArtifact(BaseModel):
    """Which file one reviewer writes its findings to."""

    reviewer: str = Field(description="The reviewer, as its findings name it")
    filename: str = Field(description="Its artifact under a session's artifacts dir")
    quotes_absent: bool = Field(
        default=False,
        description=(
            "Whether its excerpt quotes what the draft is MISSING rather than "
            "what the draft got wrong. The two read a finished draft in "
            "opposite directions: acting on a wrong passage removes the quoted "
            "words, acting on a missing one puts them in"
        ),
    )


REVIEW_ARTIFACTS: tuple[ReviewArtifact, ...] = (
    ReviewArtifact(reviewer="factcheck", filename="review_factcheck.json"),
    ReviewArtifact(reviewer="narrative", filename="review_narrative.json"),
    ReviewArtifact(reviewer="style", filename="review_style.json"),
    ReviewArtifact(
        reviewer="coverage", filename="review_coverage.json", quotes_absent=True
    ),
    ReviewArtifact(reviewer="source_fidelity", filename="review_source_fidelity.json"),
)
"""Where each reviewer's findings land, so a yield report needs no run.

Coverage is the one that reads backwards, and it is declared here rather than
explained at the point of reading: its whole job is what is NOT in the draft,
so its excerpt is the author's specific that should be present. Scored like
the others it would report a finding nobody restored as a finding acted upon —
exactly inverting the reviewer whose findings go most ignored.
"""


class ReviewerYield(BaseModel):
    """What one reviewer raised on a run, and how much of it landed.

    ``acted_on`` counts findings whose quoted passage is gone from the final
    draft. That is a proxy and it is named as one: a rewrite that changed a
    sentence for its own reasons scores as having acted, and one that fixed a
    fact without disturbing the quoted words scores as having ignored. It is
    the only signal on disk that does not need both drafts re-read by a model,
    and across enough findings its direction is informative where a single row
    is not.
    """

    reviewer: str = Field(description="Which reviewer this counts")
    raised: int = Field(default=0, description="Findings it recorded")
    critical: int = Field(default=0, description="Of those, ranked critical")
    quoted: int = Field(default=0, description="Findings carrying a passage to match")
    acted_on: int = Field(
        default=0, description="Quoted findings whose passage the final draft dropped"
    )
    measured: Literal["recorded", "inferred"] = Field(
        default="inferred",
        description=(
            "'recorded' where the rewrite left a disposition per finding and "
            "this counts it; 'inferred' where it did not and this falls back "
            "to whether the quoted passage survived"
        ),
    )

    @property
    def landed(self) -> str:
        """Share of quoted findings the draft acted on, or why there is none."""
        if not self.quoted:
            return "none quoted"
        return f"{round(100 * self.acted_on / self.quoted)}%"


def quotes_absent(reviewer: str) -> bool:
    """Whether this reviewer's excerpt is what the draft LACKS."""
    return any(
        artifact.quotes_absent
        for artifact in REVIEW_ARTIFACTS
        if artifact.reviewer == reviewer
    )


def reviewer_yield(
    findings: list[ReviewFinding], recorded: RewriteDispositions, final_draft: str
) -> list[ReviewerYield]:
    """Per reviewer: what reached the rewrite, and what the rewrite did with it.

    Counts against the consolidated findings the rewrite was actually handed,
    not the per-reviewer artifacts, because that is what its tags number and
    what it answered. Where the run recorded a disposition per tag, this is the
    rewrite's own account; where it did not, it falls back to whether the
    quoted passage survived and says so, which is what lets runs predating the
    record still be read.
    """
    answered = {entry.tag: entry.action for entry in recorded.dispositions}

    def acted(index: int, finding: ReviewFinding) -> bool:
        tag = finding_tag(index)
        if tag in answered:
            return answered[tag] in ("applied", "folded")
        present = finding.text_excerpt in final_draft
        return present if quotes_absent(finding.reviewer) else not present

    def counted(reviewer: str) -> ReviewerYield:
        mine = [(i, f) for i, f in enumerate(findings) if f.reviewer == reviewer]
        quoted = [(i, f) for i, f in mine if f.text_excerpt.strip()]
        return ReviewerYield(
            reviewer=reviewer,
            raised=len(mine),
            critical=sum(1 for _, f in mine if f.severity == "critical"),
            quoted=len(quoted),
            acted_on=sum(1 for i, f in quoted if acted(i, f)),
            measured="recorded" if recorded.dispositions else "inferred",
        )

    seen = list(dict.fromkeys(f.reviewer for f in findings))
    return [counted(reviewer) for reviewer in seen]


class SessionYield(BaseModel):
    """One reviewer's yield on one run, carrying which run it was."""

    session_id: str = Field(description="The run this counts")
    counted: ReviewerYield = Field(description="What that reviewer did on it")


def final_draft_of(notes_dir: Path) -> str:
    """The text a run finished with, empty when it never reached one.

    The run's ``output``, which is the draft after the rewrite — never
    ``merged``, which is the draft the reviewers were reading. Comparing a
    finding against the text it was written about answers whether the passage
    existed, not whether anything was done about it, and scores every reviewer
    at zero.
    """
    snapshot = notes_dir / "snapshot_rewrite.json"
    if not snapshot.exists():
        return ""
    try:
        finished = PipelineSnapshot.model_validate_json(
            snapshot.read_text(encoding="utf-8")
        ).output
    except (ValidationError, ValueError):
        logger.warning("Unreadable rewrite snapshot at %s", snapshot)
        return ""
    return finished.content if finished else ""


def dispositions_of(artifacts: Path) -> RewriteDispositions:
    """What the run's rewrite recorded, empty for one that recorded nothing."""
    path = artifacts / "dispositions.json"
    if not path.exists():
        return RewriteDispositions()
    try:
        return RewriteDispositions.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError):
        logger.warning("Unreadable dispositions at %s", path)
        return RewriteDispositions()


def collect_review_yield(session: str | None) -> Iterator[SessionYield]:
    """Every reviewer's yield across the runs on disk, or one named run."""
    for session_dir in iter_session_dirs(session) if session else iter_session_dirs():
        notes_dir = pipeline_notes_dir(session_dir)
        snapshot = notes_dir / "snapshot_rewrite.json"
        draft = final_draft_of(notes_dir)
        if not draft or not snapshot.exists():
            continue
        findings = PipelineSnapshot.model_validate_json(
            snapshot.read_text(encoding="utf-8")
        ).findings
        recorded = dispositions_of(notes_dir / "artifacts")
        for counted in reviewer_yield(findings, recorded, draft):
            yield SessionYield(session_id=session_dir.name, counted=counted)


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

    @app.command("review-yield")
    def review_yield_cmd(
        session: Annotated[
            str | None,
            typer.Option("--session", "-s", help="Session id (default: every session)"),
        ] = None,
        as_json: JSON_OPT = False,
    ) -> None:
        """Per reviewer, per run: what it raised and what the final draft did.

        The question a cost decision needs and no cost table answers — a stage
        that raised forty findings the rewrite ignored is not cheap at any
        price, and one that raised three the rewrite acted on every time is not
        expensive. Reads finished runs on disk, so it costs nothing to ask.
        """
        rows = list(collect_review_yield(session))
        if not rows:
            typer.echo("No finished run has both reviewer findings and a draft")
            raise typer.Exit(1)
        if as_json:
            output_json(rows)
            return
        typer.echo(
            format_table(
                ["session", "reviewer", "raised", "critical", "quoted", "acted", "%"],
                [
                    [
                        row.session_id[:8],
                        row.counted.reviewer,
                        str(row.counted.raised),
                        str(row.counted.critical),
                        str(row.counted.quoted),
                        str(row.counted.acted_on),
                        row.counted.landed,
                    ]
                    for row in rows
                ],
            )
        )

    return app
