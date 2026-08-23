"""Author feedback intake — what reaches the run, and what is not lost on the way.

Three different events used to look identical from inside the pipeline: an
author who said nothing, a Drive that could not be read, and a comment dropped
between the fetch and the note file. These tests hold them apart — every page
of a multi-page document reaches the caller, an unreadable Drive arrives as a
typed failure rather than an empty list, and a comment whose recording fails is
offered again by the next poll. The self-feedback guard (a run must not ingest
its own questions as author feedback) is pinned alongside, because every
document now polls through the same path.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Unpack

import pytest

from lup.mcp import LupMcpTool, ToolError
from lup.types import JsonObject, JsonValue

import inkwell.agent.tools.google_docs as google_docs
from inkwell.agent import client
from inkwell.agent.google_auth import (
    CommentsResource,
    DriveService,
    FilesResource,
    GoogleRequest,
    PermissionsResource,
    RepliesResource,
    ServiceFactory,
    CommentQuery,
)
from inkwell.agent.models import ClassifiedComment
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import PipelineListener, PipelineRunner
from inkwell.agent.session import AuthorComment, WritingSessionState
from inkwell.agent.tools.google_docs_schema import Comment
import inkwell.agent.watcher as watcher_module
from inkwell.agent.watcher import (
    ClaimHold,
    PollInput,
    create_source_watcher_tools,
    create_watcher_tools,
)


def page_payload(comments: list[JsonValue], next_token: str) -> JsonObject:
    """One Drive page of comments, naming the page after it when there is one."""
    if not next_token:
        return {"comments": comments}
    return {"comments": comments, "nextPageToken": next_token}


def comment_pages(*groups: list[JsonValue]) -> dict[str, JsonValue]:
    """Canned Drive responses, each page pointing at the next one by token."""
    return {
        ("" if index == 0 else f"page-{index}"): page_payload(
            comments, f"page-{index + 1}" if index + 1 < len(groups) else ""
        )
        for index, comments in enumerate(groups)
    }


class CannedPage:
    """One canned Drive answer."""

    def __init__(self, payload: JsonValue) -> None:
        self.payload = payload

    def execute(self) -> JsonValue:
        return self.payload


class FailedCall:
    """A Drive call that raises instead of answering."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def execute(self) -> JsonValue:
        raise self.failure


class CannedComments:
    """Drive's comments resource, answering from canned pages by token."""

    def __init__(
        self, pages: dict[str, JsonValue], failure: Exception | None = None
    ) -> None:
        self.pages = pages
        self.failure = failure

    def list(self, **query: Unpack[CommentQuery]) -> GoogleRequest:
        if self.failure is not None:
            return FailedCall(self.failure)
        token = query["pageToken"] if "pageToken" in query else ""
        return CannedPage(self.pages[token])

    def create(self, *, fileId: str, body: JsonObject, fields: str) -> GoogleRequest:
        raise NotImplementedError("canned comments serve reads only")


class CannedDrive:
    """A Drive service answering comment reads from canned pages.

    Only ``comments()`` is reached by an intake poll; the rest of the service
    is declared so this stands in for the real one, and raises if anything
    reaches for it.
    """

    def __init__(self, comments_resource: CannedComments) -> None:
        self.comments_resource = comments_resource

    def comments(self) -> CommentsResource:
        return self.comments_resource

    def replies(self) -> RepliesResource:
        raise NotImplementedError("canned drive serves comment reads only")

    def permissions(self) -> PermissionsResource:
        raise NotImplementedError("canned drive serves comment reads only")

    def files(self) -> FilesResource:
        raise NotImplementedError("canned drive serves comment reads only")


class CannedServices(ServiceFactory):
    """The service factory a test hands the poller, over one canned Drive."""

    def __init__(self, drive: CannedDrive) -> None:
        super().__init__(token_path="")
        self.drive = drive

    def drive_service(self) -> DriveService:
        return self.drive


def serve_comments(
    monkeypatch: pytest.MonkeyPatch,
    *groups: list[JsonValue],
    failure: Exception | None = None,
) -> None:
    """Answer every comment read in this test from canned pages."""
    services = CannedServices(
        CannedDrive(CannedComments(comment_pages(*groups), failure))
    )
    monkeypatch.setattr(google_docs, "services", lambda: services)


def comment(payload: JsonValue) -> Comment:
    """One Drive comment, read into the shape a filter sees."""
    return Comment.model_validate(payload)


def ids(comments: list[AuthorComment]) -> list[str]:
    """The comment ids an intake handed over, in order."""
    return [entry["comment_id"] for entry in comments]


def watched(doc_id: str = "doc") -> WritingSessionState:
    """A session state already attached to a document."""
    state = WritingSessionState()
    state.set_doc(doc_id, f"https://docs.google.com/document/d/{doc_id}/edit")
    return state


def tool_named(tools: list[LupMcpTool], name: str) -> LupMcpTool:
    """The watcher tool answering to `name`."""
    return next(tool for tool in tools if tool.name == name)


def classifier(
    verdict: ClassifiedComment | None,
) -> Callable[..., Awaitable[ClassifiedComment | None]]:
    """A stand-in classifier that answers with `verdict`, or fails to answer."""

    async def classify(*_args: object, **_kwargs: object) -> ClassifiedComment | None:
        return verdict

    return classify


def stage_local() -> ClassifiedComment:
    """A classification the watcher would record."""
    return ClassifiedComment(comment_id="", content="", impact="stage_local")


async def quiet(_failure: str) -> None:
    """An unreachable report nobody is listening to."""


class TestAuthorNews:
    """Which comments on the output doc count as the author speaking."""

    def test_agent_comment_without_reply_is_not_news(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")

        assert (
            state.author_news(
                comment({"id": "c1", "content": "[STYLE] cut filler", "replies": []})
            )
            is None
        )

    def test_agent_comment_with_agent_only_reply_is_not_news(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")
        state.mark_agent_comment("r1")

        assert (
            state.author_news(
                comment(
                    {
                        "id": "c1",
                        "content": "[QUESTION] msd or lsd?",
                        "replies": [{"id": "r1", "content": "Noted."}],
                    }
                )
            )
            is None
        )

    def test_agent_question_with_author_reply_surfaces_author_text(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")
        state.mark_agent_comment("r1")

        news = state.author_news(
            comment(
                {
                    "id": "c1",
                    "content": "[QUESTION] msd or lsd?",
                    "replies": [
                        {"id": "r1", "content": "Noted."},
                        {"id": "r2", "content": "lsd-first, see chapter 2"},
                    ],
                }
            )
        )

        assert news is not None
        assert news["reply"] == "lsd-first, see chapter 2"

    def test_author_comment_is_news(self) -> None:
        news = WritingSessionState().author_news(
            comment({"id": "c9", "content": "wrong convention", "replies": []})
        )

        assert news is not None
        assert news["content"] == "wrong convention"


class TestPagination:
    """Past the first page is where a hundred-comment document keeps the rest."""

    async def test_every_page_reaches_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(
            monkeypatch,
            [{"id": "c1", "content": "one"}, {"id": "c2", "content": "two"}],
            [{"id": "c3", "content": "three"}],
            [{"id": "c4", "content": "four"}, {"id": "c5", "content": "five"}],
        )

        intake = await watched().poll_author_comments()

        assert intake.reached
        assert ids(intake.comments) == ["c1", "c2", "c3", "c4", "c5"]

    async def test_a_single_page_needs_no_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "only"}])

        assert ids((await watched().poll_author_comments()).comments) == ["c1"]

    async def test_resolved_comments_are_left_out_of_every_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(
            monkeypatch,
            [{"id": "c1", "content": "settled", "resolved": True}],
            [{"id": "c2", "content": "open"}],
        )

        assert ids((await watched().poll_author_comments()).comments) == ["c2"]


class TestUnreachableDrive:
    """An unreadable document is a failure, never an author who said nothing."""

    async def test_a_failed_call_arrives_as_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, failure=OSError("network unreachable"))

        intake = await watched().poll_author_comments()

        assert not intake.reached
        assert "network unreachable" in intake.unreachable
        assert intake.comments == []

    async def test_an_unreadable_payload_arrives_as_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services = CannedServices(
            CannedDrive(CannedComments({"": {"comments": "not a list"}}))
        )
        monkeypatch.setattr(google_docs, "services", lambda: services)

        assert not (await watched().poll_author_comments()).reached

    async def test_no_document_is_reached_and_quiet(self) -> None:
        intake = await WritingSessionState().poll_author_comments()

        assert intake.reached
        assert intake.comments == []

    async def test_the_probe_reports_an_unreachable_drive_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, failure=TimeoutError("drive timed out"))

        assert not (await watched().probe_author_comments()).reached

    async def test_the_probe_counts_without_claiming(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "unread"}])
        state = watched()

        probed = await state.probe_author_comments()

        assert ids(probed.comments) == ["c1"]
        assert ids((await state.poll_author_comments()).comments) == ["c1"]


class TestRecordBeforeSeen:
    """A comment is accounted for by whoever writes it down, not by the poll."""

    async def test_a_poll_accounts_for_nothing_on_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "fix the intro"}])
        state = watched()

        await state.poll_author_comments()

        assert state.seen_comments.handled == []

    async def test_recording_a_comment_accounts_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "fix the intro"}])
        state = watched()
        await state.poll_author_comments()

        with state.seen_comments.recording("c1"):
            pass

        assert state.seen_comments.handled == ["c1"]
        assert (await state.poll_author_comments()).comments == []

    async def test_a_failure_before_the_record_re_offers_the_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "fix the intro"}])
        state = watched()
        assert ids((await state.poll_author_comments()).comments) == ["c1"]

        with pytest.raises(RuntimeError):
            with state.seen_comments.recording("c1"):
                raise RuntimeError("classified, then died before the note was written")

        assert state.seen_comments.handled == []
        assert ids((await state.poll_author_comments()).comments) == ["c1"]

    async def test_a_comment_in_flight_is_not_offered_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "fix the intro"}])
        state = watched()

        assert ids((await state.poll_author_comments()).comments) == ["c1"]
        assert (await state.poll_author_comments()).comments == []

    async def test_an_unrecorded_comment_stays_pending_in_the_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "fix the intro"}])
        state = watched()
        state.attach_records(tmp_path)

        await state.poll_author_comments()

        assert state.records is not None
        outcome = state.records.outcomes()[0]
        assert outcome.unresolved == ["c1"]
        assert outcome.unreachable == ""


class TestSourceIntake:
    """The source document polls the same way, on its own ledger."""

    async def test_source_comments_are_offered_then_accounted_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(
            monkeypatch,
            [{"id": "s1", "content": "this section is the point"}],
            [{"id": "s2", "content": "drop this aside", "resolved": True}],
        )
        state = WritingSessionState()
        state.source_doc_id = "source-doc"

        assert ids((await state.poll_source_comments()).comments) == ["s1"]
        with state.seen_source_comments.recording("s1"):
            pass

        assert (await state.poll_source_comments()).comments == []
        assert state.seen_comments.handled == []

    async def test_an_unreachable_source_doc_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, failure=OSError("source doc unreadable"))
        state = WritingSessionState()
        state.source_doc_id = "source-doc"

        assert not (await state.poll_source_comments()).reached


class TestAgentIdsRegistry:
    def test_registry_survives_process_restart(self, tmp_path: Path) -> None:
        first = WritingSessionState()
        first.attach_records(tmp_path)
        first.mark_agent_comment("c1")
        first.mark_agent_comment("r1")

        second = WritingSessionState()
        second.attach_records(tmp_path)

        assert "c1" in second.agent_comments
        assert "r1" in second.agent_comments

    def test_stale_snapshot_merges_with_registry(self, tmp_path: Path) -> None:
        first = WritingSessionState()
        first.attach_records(tmp_path)
        first.mark_agent_comment("review-comment")

        resumed = WritingSessionState()
        resumed.attach_records(tmp_path)
        resumed.agent_comments.mark("older-snapshot-id")

        assert "review-comment" in resumed.agent_comments
        assert "older-snapshot-id" in resumed.agent_comments

    def test_an_agent_comment_does_not_become_author_feedback_on_resume(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first = WritingSessionState()
        first.attach_records(tmp_path)
        first.mark_agent_comment("q1")

        resumed = watched()
        resumed.attach_records(tmp_path)

        assert (
            resumed.author_news(
                comment({"id": "q1", "content": "[QUESTION] which convention?"})
            )
            is None
        )

    def test_a_recorded_comment_stays_accounted_for_across_restart(
        self, tmp_path: Path
    ) -> None:
        first = WritingSessionState()
        first.attach_records(tmp_path)
        with first.seen_comments.recording("c1"):
            pass

        resumed = WritingSessionState()
        resumed.attach_records(tmp_path)

        assert "c1" in resumed.seen_comments


class TestWatcherIngest:
    """The watcher's own tool: record first, account after, drop nothing between.

    This is the every-30-seconds path, and the whole of it — classification
    included — sits inside the claim, because a classification that fails is
    exactly when a comment would otherwise be filtered out of every later poll
    with nothing written down.
    """

    def watcher_tools(
        self, state: WritingSessionState, notes: PipelineNotes
    ) -> list[LupMcpTool]:
        """The output-document watcher's tools, over this state and notes."""
        return create_watcher_tools(
            session_state=state, notes=notes, plan_breaking_signal=asyncio.Event()
        )

    async def test_a_failed_classification_re_offers_the_comment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "cut section 3"}])
        state = watched()
        notes = PipelineNotes(tmp_path / "n")
        classify = tool_named(self.watcher_tools(state, notes), "poll_and_classify")
        assert ids((await state.poll_author_comments()).comments) == ["c1"]

        monkeypatch.setattr(watcher_module, "query", classifier(None))
        with pytest.raises(ToolError):
            await classify(PollInput(comment_id="c1", content="cut section 3"))

        assert state.seen_comments.handled == []
        assert list(notes.comments_dir.glob("*.json")) == []
        assert ids((await state.poll_author_comments()).comments) == ["c1"]

    async def test_a_recorded_comment_is_accounted_for(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "cut section 3"}])
        state = watched()
        notes = PipelineNotes(tmp_path / "n")
        classify = tool_named(self.watcher_tools(state, notes), "poll_and_classify")
        await state.poll_author_comments()

        monkeypatch.setattr(watcher_module, "query", classifier(stage_local()))
        await classify(PollInput(comment_id="c1", content="cut section 3"))

        assert state.seen_comments.handled == ["c1"]
        assert len(list(notes.comments_dir.glob("*.json"))) == 1
        assert (await state.poll_author_comments()).comments == []

    async def test_the_note_is_written_before_the_comment_is_accounted_for(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "cut section 3"}])
        state = watched()
        notes = PipelineNotes(tmp_path / "n")
        classify = tool_named(self.watcher_tools(state, notes), "poll_and_classify")
        await state.poll_author_comments()
        monkeypatch.setattr(watcher_module, "query", classifier(stage_local()))
        unaccounted_at_write = False

        async def record(_classified: ClassifiedComment) -> None:
            """Stand in for the note write, reading the ledger as it happens."""
            nonlocal unaccounted_at_write
            unaccounted_at_write = "c1" not in state.seen_comments.handled

        monkeypatch.setattr(notes, "add_comment", record)
        await classify(PollInput(comment_id="c1", content="cut section 3"))

        assert unaccounted_at_write
        assert state.seen_comments.handled == ["c1"]

    async def test_the_source_tool_accounts_on_its_own_ledger(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = WritingSessionState()
        state.source_doc_id = "source-doc"
        classify = tool_named(
            create_source_watcher_tools(
                session_state=state,
                notes=PipelineNotes(tmp_path / "n"),
                plan_breaking_signal=asyncio.Event(),
            ),
            "poll_and_classify",
        )

        monkeypatch.setattr(watcher_module, "query", classifier(stage_local()))
        await classify(PollInput(comment_id="s1", content="this is the point"))

        assert state.seen_source_comments.handled == ["s1"]
        assert state.seen_comments.handled == []


class TestWatcherClaims:
    """No claim outlives the turn that was supposed to record its comment."""

    async def test_a_turn_that_records_nothing_releases_the_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "cut section 3"}])
        state = watched()
        claims = ClaimHold(state.seen_comments)

        claims.hold(ids((await state.poll_author_comments()).comments))
        assert (await state.poll_author_comments()).comments == []

        claims.release()

        assert state.seen_comments.handled == []
        assert ids((await state.poll_author_comments()).comments) == ["c1"]

    async def test_the_release_leaves_a_recorded_comment_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(
            monkeypatch,
            [{"id": "c1", "content": "cut section 3"}, {"id": "c2", "content": "cite"}],
        )
        state = watched()
        claims = ClaimHold(state.seen_comments)
        claims.hold(ids((await state.poll_author_comments()).comments))

        with state.seen_comments.recording("c1"):
            pass
        claims.release()

        assert ids((await state.poll_author_comments()).comments) == ["c2"]

    async def test_a_batch_dropped_before_its_turn_is_released_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_comments(
            monkeypatch,
            [{"id": "c1", "content": "cut section 3"}, {"id": "c2", "content": "cite"}],
        )
        state = watched()
        claims = ClaimHold(state.seen_comments)

        claims.hold(["c1"])
        state.seen_comments.claim(["c1"])
        claims.hold(["c2"])
        state.seen_comments.claim(["c2"])
        claims.release()

        assert ids((await state.poll_author_comments()).comments) == ["c1", "c2"]

    async def test_the_watcher_holds_claims_on_the_session_ledger(
        self, tmp_path: Path
    ) -> None:
        state = watched()
        with client.AgentSurface().activate():
            watcher = watcher_module.create_comment_watcher(
                session_state=state,
                notes=PipelineNotes(tmp_path / "n"),
                plan_breaking_signal=asyncio.Event(),
                report_unreachable=quiet,
            )

        state.seen_comments.claim(["c1"])
        watcher.claims.hold(["c1"])
        watcher.claims.release()

        assert "c1" not in state.seen_comments


class RecordingListener(PipelineListener):
    """A listener keeping everything the author would have been shown."""

    def __init__(self) -> None:
        super().__init__()
        self.shown = ""

    async def on_progress(self, message: str) -> None:
        self.shown = f"{self.shown}\n{message}"


class TestPipelineIngest:
    """What the run does with an intake it cannot complete."""

    async def test_a_failed_ingest_leaves_the_comment_for_the_next_poll(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        serve_comments(monkeypatch, [{"id": "c1", "content": "cut section 3"}])
        runner = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), session_state=watched()
        )

        async def die(_comment: AuthorComment) -> None:
            raise RuntimeError("classifier died before the note was written")

        monkeypatch.setattr(runner, "classify_and_record_comment", die)
        with pytest.raises(RuntimeError):
            await runner.gather_feedback()

        assert runner.state.seen_comments.handled == []
        assert ids((await runner.state.poll_author_comments()).comments) == ["c1"]

    async def test_an_unreachable_drive_is_reported_to_the_author(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        serve_comments(monkeypatch, failure=OSError("token expired"))
        listener = RecordingListener()
        runner = PipelineRunner(
            sources=["x"],
            notes=PipelineNotes(tmp_path / "n"),
            session_state=watched(),
            listener=listener,
        )

        assert await runner.gather_feedback() == 0
        assert "Could not read comments" in listener.shown
        assert "token expired" in listener.shown
