"""A book's identity, and the record that outlives the run that wrote it.

A chapter is one run. What it leaves for the chapters beside it has to be on
disk somewhere no session owns, addressed by something the next run can spell
without guessing. These tests pin both halves: the identity a plan carries and
what its absence means, the record's location and its empty read, and the
partition that keeps two concurrent chapter runs off each other's files.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import inkwell.environment.launch as launch_module
from inkwell.agent import config as config_mod
from inkwell.agent.book import (
    BookRecord,
    BookStore,
    ChapterPlacement,
    ChapterRecord,
)
from inkwell.agent.client import CostAccumulator
from inkwell.agent.core import SessionTrace
from inkwell.agent.models import (
    AgentSessionResult,
    ArticlePlan,
    SectionPlan,
    WritingOutput,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    PipelineListener,
    PipelineRunner,
    plan_article,
    refine_plan,
)
from inkwell.agent.reader_feedback import (
    ReaderFeedback,
    ReaderFeedbackTree,
    SectionAddresses,
    ingest_reader_feedback,
)
from inkwell.agent.session import WritingSessionState
from inkwell.environment.entrypoints import EntryPointValues
from inkwell.environment.launch import run_declared_session

EXPORT = Path(__file__).parent / "data" / "formspree_export_sample.json"
"""The reader-feedback export the routing tests measure against — the same
fixture ``test_reader_feedback`` reads, keyed by chapter and section."""


type LaunchArgument = (
    str
    | bool
    | list[str]
    | None
    | ChapterPlacement
    | PipelineListener
    | SessionTrace
    | WritingSessionState
    | CostAccumulator
)
"""Anything the launch path hands the pipeline: a value the author declared, or
one of the run's collaborators."""


class PlacedLaunch(BaseModel):
    """Where the launch path told the pipeline this run sits."""

    placement: ChapterPlacement | None = None


def plan_for(chapter: int | None, *titles: str) -> ArticlePlan:
    """A plan placed in a chapter, or a standalone one placed nowhere."""
    placement = (
        None if chapter is None else ChapterPlacement(book="textbook", chapter=chapter)
    )
    return ArticlePlan(
        title="Adaptation",
        thesis="Systems adapt or break",
        target_format="blog",
        placement=placement,
        sections=[
            SectionPlan(title=title, summary="", key_points=[])
            for title in titles or ("Why",)
        ],
        research_questions=[],
        source_quotes=[],
        author_direction="",
        voice_notes="",
    )


def chapter_of(book: str, ordinal: int, title: str = "") -> ChapterRecord:
    """One chapter's record, carrying only what a test is about."""
    return ChapterRecord(
        placement=ChapterPlacement(book=book, chapter=ordinal),
        title=title or f"Chapter {ordinal}",
    )


CHAPTER_3 = ChapterPlacement(book="textbook", chapter=3)
"""The placement the stage tests launch their run with."""


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BookStore:
    """The book store every part of the run resolves, pointed at a temp root."""
    root = tmp_path / "books"
    monkeypatch.setattr(config_mod.settings, "books_path", str(root))
    return BookStore(root=root)


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


async def capture_plan_task(
    notes: PipelineNotes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    placement: ChapterPlacement | None,
    titles: tuple[str, ...] = (),
) -> list[str]:
    """Run the plan stage against a planner that only records its task text."""
    tasks: list[str] = []

    async def capture(task: str, **_kwargs: object) -> None:
        tasks.append(task)
        notes.save_artifact("plan", plan_for(None, *titles))

    monkeypatch.setattr("inkwell.agent.pipeline.query", capture)
    await plan_article(notes, placement=placement)
    return tasks


async def run_plan_stage(
    notes: PipelineNotes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    placement: ChapterPlacement | None,
    titles: tuple[str, ...] = (),
) -> ArticlePlan:
    """The plan stage, against a planner that writes a plan carrying no identity.

    Carrying none is the point: the planner is never asked to invent a book id,
    so whatever the produced plan says about its book, the run put there.
    """
    await capture_plan_task(notes, monkeypatch, placement=placement, titles=titles)
    planned = notes.load_artifact("plan", ArticlePlan)
    assert planned is not None
    return planned


async def run_refine_stage(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> ArticlePlan:
    """The refine stage, against a refiner that rebuilds the plan from scratch."""

    async def refine(_task: str, **_kwargs: object) -> None:
        (notes.artifacts_dir / "plan_refined.json").write_text(
            plan_for(None)
            .model_copy(update={"title": "Adaptation, refined"})
            .model_dump_json(),
            encoding="utf-8",
        )

    monkeypatch.setattr("inkwell.agent.pipeline.query", refine)
    return await refine_plan(notes)


class TestIdentityOnThePlan:
    """What a plan says about which chapter of which book it is."""

    def test_a_plan_carries_which_chapter_of_which_book_it_is(self) -> None:
        plan = plan_for(3)
        assert plan.placement is not None
        assert (plan.placement.book, plan.placement.chapter) == ("textbook", 3)

    def test_a_plan_carrying_no_placement_is_valid_and_bookless(self) -> None:
        """A standalone article is not silently promoted to a book of one."""
        assert plan_for(None).placement is None

    def test_a_plan_round_trips_its_placement_through_json(self) -> None:
        restored = ArticlePlan.model_validate_json(plan_for(3).model_dump_json())
        assert restored.placement == ChapterPlacement(book="textbook", chapter=3)

    def test_a_book_identity_that_could_name_another_directory_is_refused(
        self,
    ) -> None:
        """The identity names a directory, so no book id can walk out of it."""
        with pytest.raises(ValidationError):
            ChapterPlacement(book="../elsewhere", chapter=1)

    def test_a_chapter_ordinal_starts_at_one(self) -> None:
        with pytest.raises(ValidationError):
            ChapterPlacement(book="textbook", chapter=0)

    def test_the_address_book_reads_the_chapter_off_the_identity(self) -> None:
        placed = SectionAddresses.for_plan(plan_for(3)).entry_for("Why")
        unplaced = SectionAddresses.for_plan(plan_for(None)).entry_for("Why")
        assert placed is not None and placed.chapter == 3
        assert unplaced is not None and unplaced.chapter is None


class TestReadingABookNobodyHasWrittenTo:
    """The first chapter of a book takes the path the ninth takes."""

    def test_a_book_with_no_record_reads_as_an_empty_one(
        self, store: BookStore
    ) -> None:
        assert store.load("textbook") == BookRecord(book="textbook")

    def test_an_empty_read_does_not_create_the_store(self, store: BookStore) -> None:
        store.load("textbook")
        assert not store.root.exists()

    def test_a_store_with_no_root_holds_no_books(self, store: BookStore) -> None:
        assert store.books() == ()

    def test_an_empty_record_answers_every_question_a_full_one_does(
        self, store: BookStore
    ) -> None:
        empty = store.load("textbook")
        assert empty.chapter(1) is None
        assert empty.besides(ChapterPlacement(book="textbook", chapter=1)) == empty
        assert "0 chapter(s) on record" in empty.render()


class TestWhatOneChapterLeavesForTheNext:
    """A record written by one run, read by a run that did not create it."""

    def test_a_chapter_run_reads_what_an_earlier_one_wrote(
        self, store: BookStore
    ) -> None:
        store.publish(chapter_of("textbook", 1, "Foundations"))
        later = BookStore(root=store.root).load("textbook").chapter(1)
        assert later is not None
        assert later.title == "Foundations"
        assert later.placement == ChapterPlacement(book="textbook", chapter=1)

    def test_the_record_is_addressed_by_the_books_identity(
        self, store: BookStore
    ) -> None:
        written = store.publish(chapter_of("textbook", 12))
        assert written == store.root / "textbook" / "012.json"

    def test_chapters_read_back_in_chapter_order(self, store: BookStore) -> None:
        for ordinal in (11, 2, 1):
            store.publish(chapter_of("textbook", ordinal))
        record = store.load("textbook")
        assert [held.placement.chapter for held in record.chapters] == [1, 2, 11]

    def test_a_run_reads_its_siblings_rather_than_its_own_stale_record(
        self, store: BookStore
    ) -> None:
        for ordinal in (1, 2, 3):
            store.publish(chapter_of("textbook", ordinal))
        mine = ChapterPlacement(book="textbook", chapter=2)
        siblings = store.load("textbook").besides(mine)
        assert [held.placement.chapter for held in siblings.chapters] == [1, 3]

    def test_another_books_chapter_two_drops_nothing_from_this_one(
        self, store: BookStore
    ) -> None:
        for ordinal in (1, 2, 3):
            store.publish(chapter_of("textbook", ordinal))
        theirs = ChapterPlacement(book="other-book", chapter=2)
        siblings = store.load("textbook").besides(theirs)
        assert [held.placement.chapter for held in siblings.chapters] == [1, 2, 3]

    def test_one_book_does_not_read_anothers_chapters(self, store: BookStore) -> None:
        store.publish(chapter_of("textbook", 1))
        store.publish(chapter_of("other-book", 1))
        assert store.books() == ("other-book", "textbook")
        assert len(store.load("other-book").chapters) == 1

    def test_a_chapter_file_that_no_longer_parses_leaves_the_others_readable(
        self, store: BookStore
    ) -> None:
        store.publish(chapter_of("textbook", 1))
        store.publish(chapter_of("textbook", 2))
        store.chapter_path(ChapterPlacement(book="textbook", chapter=1)).write_text(
            "{ not a record", encoding="utf-8"
        )
        assert [held.placement.chapter for held in store.load("textbook").chapters] == [
            2
        ]

    def test_the_rendered_record_carries_what_each_chapter_claimed(
        self, store: BookStore
    ) -> None:
        store.publish(
            ChapterRecord(
                placement=ChapterPlacement(book="textbook", chapter=1),
                title="Foundations",
                thesis="Everything rests here",
                sections=["Why", "How"],
            )
        )
        rendered = store.load("textbook").render()
        assert "Chapter 1 — Foundations" in rendered
        assert "Everything rests here" in rendered
        assert "- Why" in rendered


class TestConcurrentChapterRuns:
    """One writer per chapter file — the partition, not a lock, is the guarantee."""

    def test_two_chapters_of_one_book_never_write_the_same_file(
        self, store: BookStore
    ) -> None:
        first = store.chapter_path(ChapterPlacement(book="textbook", chapter=1))
        ninth = store.chapter_path(ChapterPlacement(book="textbook", chapter=9))
        assert first != ninth

    def test_publishing_one_chapter_leaves_a_concurrent_siblings_record_intact(
        self, store: BookStore
    ) -> None:
        store.publish(chapter_of("textbook", 1, "Foundations"))
        store.publish(chapter_of("textbook", 9, "Consequences"))
        record = store.load("textbook")
        assert [held.title for held in record.chapters] == [
            "Foundations",
            "Consequences",
        ]

    def test_re_running_one_chapter_replaces_only_its_own_record(
        self, store: BookStore
    ) -> None:
        store.publish(chapter_of("textbook", 1, "Foundations"))
        store.publish(chapter_of("textbook", 9, "Consequences"))
        store.publish(chapter_of("textbook", 1, "Foundations, again"))
        record = store.load("textbook")
        assert [held.title for held in record.chapters] == [
            "Foundations, again",
            "Consequences",
        ]

    def test_no_half_written_file_is_left_where_a_reader_would_take_it(
        self, store: BookStore
    ) -> None:
        """The atomic temp-and-rename names its temporary out of the way, so a
        reader listing the book cannot pick one up as a chapter."""
        store.publish(chapter_of("textbook", 1))
        listed = sorted(path.name for path in (store.root / "textbook").iterdir())
        assert listed == ["001.json"]


class TestTheStagesThatWriteIdentity:
    """The plan stage stamps it; refinement carries it rather than re-deriving."""

    async def test_the_plan_stage_writes_the_runs_identity_into_its_plan(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planned = await run_plan_stage(notes, monkeypatch, placement=CHAPTER_3)
        assert planned.placement == CHAPTER_3
        assert notes.load_artifact("plan", ArticlePlan) == planned

    async def test_an_unplaced_run_produces_a_bookless_plan(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planned = await run_plan_stage(notes, monkeypatch, placement=None)
        assert planned.placement is None
        assert store.books() == ()

    async def test_the_plan_stage_publishes_the_chapter_to_its_book(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await run_plan_stage(notes, monkeypatch, placement=CHAPTER_3)
        held = store.load("textbook").chapter(3)
        assert held is not None
        assert (held.title, held.sections) == ("Adaptation", ["Why"])

    async def test_a_chapter_run_is_shown_what_its_siblings_wrote(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.publish(chapter_of("textbook", 1, "Foundations"))
        tasks = await capture_plan_task(notes, monkeypatch, placement=CHAPTER_3)
        assert "[book]" in tasks[0]
        assert "Foundations" in notes.text_artifact_path("book").read_text("utf-8")

    async def test_a_standalone_run_is_shown_no_book_at_all(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.publish(chapter_of("textbook", 1, "Foundations"))
        tasks = await capture_plan_task(notes, monkeypatch, placement=None)
        assert "[book]" not in tasks[0]

    async def test_refinement_carries_the_identity_through(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refiner rebuilds the plan from its own tools, so an identity it
        was never asked for is the one that would silently go missing."""
        await run_plan_stage(notes, monkeypatch, placement=CHAPTER_3)
        refined = await run_refine_stage(notes, monkeypatch)
        assert refined.placement == CHAPTER_3
        assert refined.title == "Adaptation, refined"

    async def test_refinement_of_a_bookless_plan_stays_bookless(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await run_plan_stage(notes, monkeypatch, placement=None)
        assert (await run_refine_stage(notes, monkeypatch)).placement is None

    async def test_refinement_updates_what_the_book_holds_for_this_chapter(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await run_plan_stage(notes, monkeypatch, placement=CHAPTER_3)
        await run_refine_stage(notes, monkeypatch)
        held = store.load("textbook").chapter(3)
        assert held is not None and held.title == "Adaptation, refined"


class TestARunThatIsResumed:
    """A resumed chapter does not quietly become a standalone article."""

    def test_a_resumed_run_reads_its_placement_off_the_snapshots_plan(self) -> None:
        runner = PipelineRunner(sources=["dummy"])
        runner.snapshot.plan = plan_for(3)
        assert runner.placement == CHAPTER_3

    def test_what_the_launch_declared_outranks_the_snapshot(self) -> None:
        runner = PipelineRunner(sources=["dummy"], placement=CHAPTER_3)
        runner.snapshot.plan = plan_for(9)
        assert runner.placement == CHAPTER_3

    def test_a_run_with_neither_is_placed_nowhere(self) -> None:
        assert PipelineRunner(sources=["dummy"]).placement is None


class TestReviseOneChapterOfABook:
    """The workflow this exists for: rewriting one chapter of a book that is
    already published, on its own, without re-running the book."""

    async def test_a_placement_reaches_the_pipeline_from_the_launch_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launched = PlacedLaunch()

        async def record(
            *, placement: ChapterPlacement | None = None, **rest: LaunchArgument
        ) -> AgentSessionResult:
            launched.placement = placement
            return AgentSessionResult(
                session_id="test", timestamp="", output=WritingOutput(title="")
            )

        monkeypatch.setattr(launch_module, "run_session", record)
        await run_declared_session(
            EntryPointValues.declared(
                "revise", {"draft": "chapter4.md", "chapter": "atlas:4"}
            ),
            session_id="test",
        )
        assert launched.placement == ChapterPlacement(book="atlas", chapter=4)

    async def test_a_revised_chapter_hands_its_writers_their_readers(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity measured where it lands: a plan of plain section titles
        routes reader feedback only once the run says which chapter it is."""
        ingest_reader_feedback(ReaderFeedbackTree(root=notes.reader_dir), EXPORT)
        titles = ("Opening", "Measurement", "Intelligence")
        planned = await run_plan_stage(
            notes,
            monkeypatch,
            placement=ChapterPlacement(book="atlas", chapter=1),
            titles=titles,
        )
        placed = ReaderFeedback.for_plan(notes.reader_dir, planned)
        unplaced = ReaderFeedback.for_plan(notes.reader_dir, plan_for(None, *titles))
        assert placed.for_section("Intelligence") is not None
        assert unplaced.for_section("Intelligence") is None


class TestWhereTheRecordLives:
    """Outside the session, and the same one from every worktree."""

    def test_the_default_sits_beside_the_corpus_rather_than_in_a_session(
        self,
    ) -> None:
        assert config_mod.BOOKS_DIR.parent == config_mod.CORPUS_DIR.parent
        assert config_mod.BOOKS_DIR != config_mod.CORPUS_DIR

    def test_the_default_resolves_against_the_shared_checkout(self) -> None:
        """The same property CORPUS_DIR documents: every worktree reads one
        book, rather than each starting a fresh one under its own branch."""
        assert config_mod.BOOKS_DIR.parent == config_mod.profile_store_root()

    def test_a_caller_with_another_layout_overrides_rather_than_forks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            config_mod.settings, "books_path", str(tmp_path / "elsewhere")
        )
        assert config_mod.book_store().root == tmp_path / "elsewhere"
