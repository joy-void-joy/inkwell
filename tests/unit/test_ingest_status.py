"""The intake report — what a run took in, against what is still pending.

The report exists so that "the author said nothing" and "the poll never reached
Drive" cannot be read as one state. These tests pin that distinction in what it
prints, and pin the three sets it reconciles: note files on disk, ids accounted
for with no note to show for them, and unresolved comments the last poll saw
that nothing has recorded. Everything is written through the same calls the
pipeline uses, because the report's claim is that it reads the run's own state
rather than a second view of it.
"""

from pathlib import Path

from inkwell.agent.core import pipeline_notes_dir
from inkwell.agent.models import ClassifiedComment, CommentImpact, IntakeRecord
from inkwell.agent.session import CommentRecords, WritingSessionState
from inkwell.devtools.feedback import read_status, render


def write_note(
    base: Path, comment_id: str, impact: CommentImpact, where: str = "comments"
) -> None:
    """File one classified comment where the pipeline files it."""
    directory = base / where
    directory.mkdir(parents=True, exist_ok=True)
    note = ClassifiedComment(comment_id=comment_id, content="feedback", impact=impact)
    (directory / f"{comment_id}.json").write_text(
        note.model_dump_json(), encoding="utf-8"
    )


def account_for(base: Path, *comment_ids: str) -> None:
    """Account for these comments the way a run does — through the ledger."""
    state = WritingSessionState()
    state.attach_records(base)
    for comment_id in comment_ids:
        with state.seen_comments.recording(comment_id):
            pass


def record_poll(base: Path, record: IntakeRecord) -> None:
    """Write down how one channel's last poll went."""
    CommentRecords(base).save(record)


class TestIngestStatus:
    def test_recorded_notes_are_reported_wherever_they_are_filed(
        self, tmp_path: Path
    ) -> None:
        base = pipeline_notes_dir(tmp_path)
        write_note(base, "c1", "stage_local")
        write_note(base, "c9", "dismiss", where="processed")

        status = read_status("s1", tmp_path)

        assert [note.comment_id for note in status.recorded] == ["c1", "c9"]
        assert [note.where for note in status.recorded] == ["comments", "processed"]

    def test_an_unreachable_poll_never_reads_as_nothing_pending(
        self, tmp_path: Path
    ) -> None:
        base = pipeline_notes_dir(tmp_path)
        record_poll(
            base,
            IntakeRecord(
                channel="author",
                doc_id="doc",
                at="2026-08-11T10:00:00",
                unreachable="GoogleAuthError: token revoked",
            ),
        )

        printed = render(read_status("s1", tmp_path))

        assert "DRIVE UNREACHABLE" in printed
        assert "Pending on author: unknown" in printed
        assert "Pending: nothing —" not in printed

    def test_unresolved_comments_nothing_recorded_are_pending(
        self, tmp_path: Path
    ) -> None:
        base = pipeline_notes_dir(tmp_path)
        record_poll(
            base,
            IntakeRecord(
                channel="source",
                doc_id="src",
                at="2026-08-11T10:00:00",
                unresolved=["s1", "s2"],
            ),
        )
        write_note(base, "s1", "clarification")

        status = read_status("s1", tmp_path)

        assert status.pending == ["s2"]
        assert "Pending (1 unresolved" in render(status)

    def test_an_accounted_comment_with_no_note_file_is_named(
        self, tmp_path: Path
    ) -> None:
        base = pipeline_notes_dir(tmp_path)
        write_note(base, "c1", "stage_local")
        account_for(base, "c1", "c-lost")

        status = read_status("s1", tmp_path)

        assert status.unrecorded_seen == ["c-lost"]
        assert "no note file" in render(status)

    def test_everything_ingested_reads_as_nothing_pending(self, tmp_path: Path) -> None:
        base = pipeline_notes_dir(tmp_path)
        record_poll(
            base,
            IntakeRecord(
                channel="author",
                doc_id="doc",
                at="2026-08-11T10:00:00",
                unresolved=["c1"],
            ),
        )
        write_note(base, "c1", "stage_local")
        account_for(base, "c1")

        status = read_status("s1", tmp_path)

        assert status.pending == []
        assert status.unrecorded_seen == []
        assert "Pending: nothing" in render(status)

    def test_a_note_file_that_no_longer_parses_is_named(self, tmp_path: Path) -> None:
        base = pipeline_notes_dir(tmp_path)
        write_note(base, "c1", "stage_local")
        (base / "comments" / "broken.json").write_text("{ truncated", encoding="utf-8")

        status = read_status("s1", tmp_path)

        assert [path.name for path in status.unreadable] == ["broken.json"]
        assert "no longer parse" in render(status)

    def test_a_session_that_never_polled_says_so(self, tmp_path: Path) -> None:
        printed = render(read_status("s1", tmp_path))

        assert "no poll recorded yet" in printed
        assert "nothing recorded yet" in printed
