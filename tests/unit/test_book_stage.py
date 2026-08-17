"""The stage above the plan, and the chapter that runs without it.

A book is written one chapter per run, so the two things a chapter cannot work
out for itself — where it sits in the reading order, and what it owes the
chapters around it — are decided once, above the plan, and read by every run
after. These tests pin that stage's output, the property that makes an ordinal
safe to publish (it is an identity, never reassigned), and the path the author
asked for by name: one chapter, run on its own, against the order its book
already has.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from lup.mcp import response_text
from lup.types import JsonObject
from pydantic import BaseModel

import inkwell.environment.launch as launch_module
from inkwell.agent import config as config_mod
from inkwell.agent.book import (
    BookLayout,
    BookOutline,
    BookStore,
    ChapterAssignment,
    ChapterPlacement,
    ChapterRecord,
    ProposedChapter,
    ProposedReference,
)
from inkwell.agent.config import PIPELINE_STAGES, stage_model
from inkwell.agent.models import (
    AgentSessionResult,
    ArticlePlan,
    SectionPlan,
    WritingOutput,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    CHECKPOINT_STAGES,
    DISPLAY_STAGES,
    PipelineError,
    PipelineRunner,
    PlannedChapter,
    StageCompute,
    plan_article,
    plan_book,
    validate_checkpoint_stage,
)
from inkwell.agent.tools.stage_outputs import BookCollector, make_book_tools
from inkwell.environment.entrypoints import EntryPointValues, SuppliedValues
from inkwell.environment.launch import run_declared_session

TEXTBOOK = ChapterAssignment(book="textbook")
"""A book named without an ordinal — the half its own record can answer."""

FOUNDATIONS = ProposedChapter(
    key="foundations", title="Foundations", thesis="Everything rests here"
)
MEASUREMENT = ProposedChapter(
    key="measurement", title="Measurement", thesis="What we can count"
)
CONSEQUENCES = ProposedChapter(
    key="consequences", title="Consequences", thesis="What follows"
)

LAID_OUT = BookLayout(
    title="A Textbook",
    chapters=[FOUNDATIONS, MEASUREMENT, CONSEQUENCES],
    references=[
        ProposedReference(
            from_key="measurement",
            to_key="foundations",
            relation="depends_on",
            subject="the calibration curve",
        ),
        ProposedReference(
            from_key="consequences",
            to_key="measurement",
            relation="elaborates",
            subject="the error budget",
        ),
    ],
)
"""A book of several chapters, as the book stage lays one out: keyed, ordered
and linked, with no ordinal anywhere — the numbering is not the stage's to do."""


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BookStore:
    """The book store every part of the run resolves, pointed at a temp root."""
    root = tmp_path / "books"
    monkeypatch.setattr(config_mod.settings, "books_path", str(root))
    return BookStore(root=root)


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


async def lay_out(
    notes: PipelineNotes,
    monkeypatch: pytest.MonkeyPatch,
    layout: BookLayout,
    *,
    assignment: ChapterAssignment = TEXTBOOK,
) -> BookOutline:
    """Run the book stage against a planner that lays out exactly ``layout``."""

    async def declare(_task: str, **_kwargs: object) -> None:
        notes.save_artifact("book_layout", layout)

    monkeypatch.setattr("inkwell.agent.pipeline.query", declare)
    return await plan_book(notes, assignment=assignment)


async def plan_one_chapter(
    notes: PipelineNotes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    assignment: ChapterAssignment | None,
    title: str,
) -> PlannedChapter:
    """Run the plan stage against a planner that writes a plan carrying no book.

    Carrying none is the point: the planner is never asked which chapter it is
    writing, so wherever the run ends up placed, the run placed it.
    """
    plan = ArticlePlan(
        title=title,
        thesis="Systems adapt or break",
        target_format="blog",
        sections=[SectionPlan(title="Why", summary="", key_points=[])],
        research_questions=[],
        source_quotes=[],
        author_direction="",
        voice_notes="",
    )

    async def write_plan(_task: str, **_kwargs: object) -> None:
        notes.save_artifact("plan", plan)

    monkeypatch.setattr("inkwell.agent.pipeline.query", write_plan)
    return await plan_article(notes, assignment=assignment)


def book_shown(notes: PipelineNotes) -> str:
    """What a stage of this run was handed as the book it writes into."""
    return notes.text_artifact_path("book").read_text(encoding="utf-8")


class LaunchedRun(BaseModel):
    """What the launch path handed the pipeline for one invocation."""

    assignment: ChapterAssignment | None = None
    skipped_stages: list[str] = []


async def launched(
    monkeypatch: pytest.MonkeyPatch, entry_point: str, supplied: SuppliedValues
) -> LaunchedRun:
    """Invoke one entry point's launch path, keeping what it handed the run."""
    captured = LaunchedRun()

    async def record(
        *,
        assignment: ChapterAssignment | None = None,
        skipped_stages: list[str] | None = None,
        **_rest: object,
    ) -> AgentSessionResult:
        captured.assignment = assignment
        captured.skipped_stages = skipped_stages or []
        return AgentSessionResult(
            session_id="test", timestamp="", output=WritingOutput(title="")
        )

    monkeypatch.setattr(launch_module, "run_session", record)
    await run_declared_session(
        EntryPointValues.declared(entry_point, supplied), session_id="test"
    )
    return captured


async def run_book_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PipelineRunner:
    """The book stage as the runner runs it, with the Doc and sandbox stubbed.

    The Google Doc is a display surface and the sandbox is where the stage's
    agent would run; neither decides anything these tests are about, so the
    compute takes the path a machine without Docker already takes and what the
    stage writes down is what is measured.
    """
    runner = PipelineRunner(
        sources=["dummy"],
        notes=PipelineNotes(tmp_path / "notes"),
        assignment=TEXTBOOK,
    )

    @asynccontextmanager
    async def no_sandbox(_label: str) -> AsyncIterator[StageCompute]:
        fallback = runner.fallback_compute()
        yield StageCompute(
            servers=fallback.servers,
            tool_names=fallback.tool_names,
            output_path=tmp_path / "out.md",
            sandbox=None,
        )

    async def wrote_tab(*_args: object, **_kwargs: object) -> None:
        return None

    async def made_tab(*_args: object, **_kwargs: object) -> str:
        return "tab-id"

    async def declare(_task: str, **_kwargs: object) -> None:
        runner.ensure_notes().save_artifact("book_layout", LAID_OUT)

    monkeypatch.setattr(runner, "stage_compute", no_sandbox)
    monkeypatch.setattr("inkwell.agent.pipeline.do_write_tab", wrote_tab)
    monkeypatch.setattr("inkwell.agent.pipeline.do_create_tab", made_tab)
    monkeypatch.setattr("inkwell.agent.pipeline.query", declare)
    await runner.stage_book()
    return runner


class TestTheStageAboveThePlan:
    """A book-level stage that owns what no single chapter can decide."""

    def test_the_book_stage_runs_above_the_plan_stage(self) -> None:
        assert PIPELINE_STAGES.index("book") < PIPELINE_STAGES.index("plan")
        assert DISPLAY_STAGES.index("book") < DISPLAY_STAGES.index("plan")

    async def test_laying_a_book_out_numbers_every_chapter(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outline = await lay_out(notes, monkeypatch, LAID_OUT)

        assert [held.ordinal for held in outline.chapters] == [1, 2, 3]
        assert [held.key for held in outline.chapters] == [
            "foundations",
            "measurement",
            "consequences",
        ]

    async def test_the_order_reaches_the_books_own_record(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Written where a run months later reads it, not into this session."""
        await lay_out(notes, monkeypatch, LAID_OUT)

        recorded = BookStore(root=store.root).load("textbook").outline
        assert recorded is not None
        assert recorded.title == "A Textbook"
        assert [held.title for held in recorded.chapters] == [
            "Foundations",
            "Measurement",
            "Consequences",
        ]

    async def test_the_cross_references_reach_it_as_data_a_stage_resolves(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two ordinals and a named relation, so a stage honouring a link looks
        the chapter up rather than parsing a sentence back apart."""
        outline = await lay_out(notes, monkeypatch, LAID_OUT)

        first = outline.cross_references[0]
        assert (first.from_chapter, first.to_chapter) == (2, 1)
        assert first.relation == "depends_on"
        assert first.subject == "the calibration curve"

    async def test_a_reference_is_resolved_against_the_order_that_numbered_it(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outline = await lay_out(notes, monkeypatch, LAID_OUT)

        resolved = outline.references_from(2)
        assert [r.target.title for r in resolved] == ["Foundations"]
        assert "depends on chapter 1 (Foundations)" in resolved[0].render()

    async def test_a_stage_that_produced_no_layout_is_an_error(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty layout published would retire every chapter the book has."""

        async def say_nothing(_task: str, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr("inkwell.agent.pipeline.query", say_nothing)
        with pytest.raises(PipelineError, match="produced no output"):
            await plan_book(notes, assignment=TEXTBOOK)

    async def test_a_run_that_belongs_to_no_book_lays_none_out(
        self, store: BookStore
    ) -> None:
        """The whole of a standalone piece's book stage: it has no book."""
        runner = PipelineRunner(sources=["dummy"])

        await runner.stage_book()

        assert store.books() == ()


class TestAnOrdinalIsAnIdentity:
    """Held fixed, gaps recorded, an insertion taking a number never used.

    Readers have already seen the published numbers and the reader-feedback
    export is keyed by them, so renumbering on a reorder would silently
    re-address every row already filed against this book.
    """

    async def test_reordering_the_book_renumbers_nothing(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lay_out(notes, monkeypatch, LAID_OUT)
        backwards = LAID_OUT.model_copy(
            update={"chapters": [CONSEQUENCES, MEASUREMENT, FOUNDATIONS]}
        )

        outline = await lay_out(notes, monkeypatch, backwards)

        assert [held.key for held in outline.chapters] == [
            "consequences",
            "measurement",
            "foundations",
        ]
        assert [held.ordinal for held in outline.chapters] == [3, 2, 1]

    async def test_an_inserted_chapter_takes_a_number_never_handed_out(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lay_out(notes, monkeypatch, LAID_OUT)
        inserted = ProposedChapter(key="calibration", title="Calibration")
        grown = LAID_OUT.model_copy(
            update={"chapters": [FOUNDATIONS, inserted, MEASUREMENT, CONSEQUENCES]}
        )

        outline = await lay_out(notes, monkeypatch, grown)

        assert [held.ordinal for held in outline.chapters] == [1, 4, 2, 3]

    async def test_a_dropped_chapter_holds_its_number_rather_than_passing_it_on(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lay_out(notes, monkeypatch, LAID_OUT)
        trimmed = LAID_OUT.model_copy(
            update={"chapters": [FOUNDATIONS, CONSEQUENCES], "references": []}
        )

        outline = await lay_out(notes, monkeypatch, trimmed)

        assert [held.ordinal for held in outline.retired] == [2]
        assert outline.next_ordinal() == 4

    async def test_a_chapter_appended_on_file_is_not_numbered_over(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lone run that appended itself holds an ordinal the outline never
        saw; laying the book out again must not hand that number to anyone."""
        await lay_out(notes, monkeypatch, LAID_OUT)
        store.publish(
            ChapterRecord(
                placement=ChapterPlacement(book="textbook", chapter=4),
                title="An appendix somebody wrote alone",
            )
        )
        epilogue = ProposedChapter(key="epilogue", title="Epilogue")
        grown = LAID_OUT.model_copy(update={"chapters": [*LAID_OUT.chapters, epilogue]})

        outline = await lay_out(notes, monkeypatch, grown)

        assert [held.ordinal for held in outline.chapters] == [1, 2, 3, 5]

    async def test_a_link_survives_the_reorder_it_was_declared_before(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A link is declared by key, so it follows the chapters it names."""
        await lay_out(notes, monkeypatch, LAID_OUT)
        backwards = LAID_OUT.model_copy(
            update={"chapters": [CONSEQUENCES, MEASUREMENT, FOUNDATIONS]}
        )

        outline = await lay_out(notes, monkeypatch, backwards)

        carried = outline.cross_references[0]
        assert (carried.from_chapter, carried.to_chapter) == (2, 1)


class TestTheToolsRefuseWhatCouldNotBeResolved:
    """Told while the stage can still fix it, rather than dropped in silence."""

    async def test_one_key_names_one_chapter(self, tmp_path: Path) -> None:
        tools = {t.name: t for t in make_book_tools(BookCollector(tmp_path / "l.json"))}
        chapter: JsonObject = {"chapter": FOUNDATIONS.model_dump()}

        await tools["add_chapter"].handler(chapter)
        refused = await tools["add_chapter"].handler(chapter)

        assert "is_error" in refused
        assert "key of its own" in response_text(refused)

    async def test_a_link_to_a_chapter_nobody_declared_is_refused(
        self, tmp_path: Path
    ) -> None:
        tools = {t.name: t for t in make_book_tools(BookCollector(tmp_path / "l.json"))}
        chapter: JsonObject = {"chapter": FOUNDATIONS.model_dump()}
        link: JsonObject = {"reference": LAID_OUT.references[0].model_dump()}
        await tools["add_chapter"].handler(chapter)

        dangling = await tools["add_cross_reference"].handler(link)

        assert "is_error" in dangling
        assert "add_chapter first" in response_text(dangling)


class TestOneChapterOnItsOwn:
    """The path the author asked for by name: a chapter, without its book."""

    async def test_the_chapter_command_hands_the_run_its_book_and_its_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await launched(
            monkeypatch, "chapter", {"chapter": "atlas:4", "sources": ["ch4.md"]}
        )

        assert run.assignment == ChapterAssignment(book="atlas", chapter=4)
        assert "book" in run.skipped_stages

    async def test_a_chapter_command_run_does_not_perform_the_book_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured on the stages the launched run would execute, which is what
        the declaration is for — not on the declaration itself."""
        run = await launched(
            monkeypatch, "chapter", {"chapter": "atlas", "sources": ["ch4.md"]}
        )
        runner = PipelineRunner(
            sources=["ch4.md"],
            assignment=run.assignment,
            skipped_stages=run.skipped_stages,
        )

        assert "book" not in runner.stages_for_run()
        assert "plan" in runner.stages_for_run()

    async def test_a_book_run_still_performs_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch is the chapter command's, not everyone's."""
        run = await launched(
            monkeypatch, "write", {"sources": ["book.md"], "chapter": "atlas"}
        )
        runner = PipelineRunner(
            sources=["book.md"],
            assignment=run.assignment,
            skipped_stages=run.skipped_stages,
        )

        assert "book" in runner.stages_for_run()

    async def test_a_lone_run_is_fully_identified_by_the_books_own_record(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No book stage ran, and the plan still says which chapter it is."""
        await lay_out(notes, monkeypatch, LAID_OUT)

        planned = await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert planned.plan.placement == ChapterPlacement(book="textbook", chapter=2)
        assert planned.identity is not None
        assert planned.identity.source == "recorded"

    async def test_an_explicit_ordinal_outranks_the_record(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A title match is at best a decision the author made earlier and at
        worst a coincidence, so it never overrules one they made just now."""
        await lay_out(notes, monkeypatch, LAID_OUT)

        planned = await plan_one_chapter(
            notes,
            monkeypatch,
            assignment=ChapterAssignment(book="textbook", chapter=3),
            title="Measurement",
        )

        assert planned.plan.placement == ChapterPlacement(book="textbook", chapter=3)
        assert planned.identity is not None
        assert planned.identity.source == "declared"

    async def test_a_lone_run_leaves_the_recorded_order_exactly_as_it_found_it(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = await lay_out(notes, monkeypatch, LAID_OUT)

        await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert store.load("textbook").outline == recorded

    async def test_a_lone_run_files_its_own_chapter_beside_its_siblings(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lay_out(notes, monkeypatch, LAID_OUT)

        await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        held = store.load("textbook").chapter(2)
        assert held is not None and held.title == "Measurement"

    async def test_a_lone_run_reads_the_order_and_the_links_it_must_honour(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lay_out(notes, monkeypatch, LAID_OUT)

        await plan_one_chapter(
            notes,
            monkeypatch,
            assignment=ChapterAssignment(book="textbook", chapter=2),
            title="Measurement",
        )

        shown = book_shown(notes)
        assert "chapter 1 (Foundations)" in shown
        assert "the calibration curve" in shown
        assert "the error budget" in shown

    async def test_a_run_that_does_not_know_its_ordinal_reads_the_book_whole(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which chapter it is is not settled until its plan has a title, so
        narrowing the links to one chapter is not yet possible."""
        await lay_out(notes, monkeypatch, LAID_OUT)

        await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert "## Cross-references between chapters" in book_shown(notes)


class TestALoneRunAgainstABookWithNoRecord:
    """It succeeds, and says what it assumed rather than inventing an order."""

    async def test_it_succeeds(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planned = await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert planned.plan.placement == ChapterPlacement(book="textbook", chapter=1)

    async def test_it_says_the_ordinal_was_assumed(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planned = await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert planned.identity is not None
        assert planned.identity.source == "appended"
        assert "Assumed:" in planned.identity.render()

    async def test_what_it_says_names_the_two_ways_to_settle_it(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An assumption the author cannot act on is a shrug with punctuation."""
        planned = await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert planned.identity is not None
        said = planned.identity.render()
        assert "--chapter textbook:<n>" in said
        assert "book stage" in said

    async def test_it_appends_past_the_chapters_already_on_file(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two lone runs of a book nobody laid out must not collide."""
        store.publish(
            ChapterRecord(
                placement=ChapterPlacement(book="textbook", chapter=1),
                title="Foundations",
            )
        )

        planned = await plan_one_chapter(
            notes, monkeypatch, assignment=TEXTBOOK, title="Measurement"
        )

        assert planned.plan.placement == ChapterPlacement(book="textbook", chapter=2)

    async def test_the_absent_order_is_said_where_the_stage_reads_it(
        self, notes: PipelineNotes, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.publish(
            ChapterRecord(
                placement=ChapterPlacement(book="textbook", chapter=1),
                title="Foundations",
            )
        )

        await plan_one_chapter(
            notes,
            monkeypatch,
            assignment=ChapterAssignment(book="textbook", chapter=2),
            title="Measurement",
        )

        assert "no recorded order yet" in book_shown(notes)


class TestTheStageIsResumableLikeEveryOther:
    """A checkpoint, a stop point, a resume point and a restart point."""

    def test_a_run_can_be_paused_after_it(self, tmp_path: Path) -> None:
        runner = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="BOOK"
        )

        assert runner.stop_after == "book"

    def test_a_run_can_be_picked_up_from_it_and_regenerated_from_it(self) -> None:
        """One rule serves the stop point, the resume point and the restart
        point, so a stage every surface accepts is a stage all three take."""
        assert validate_checkpoint_stage("book", what="from_stage") == "book"
        assert "book" in CHECKPOINT_STAGES

    def test_its_model_is_the_strongest_default_rather_than_a_cheaper_tier(
        self,
    ) -> None:
        """No stage declares a cheaper model without a reason stated with it,
        and this one has no such reason: it decides the shape of a whole book."""
        assert stage_model("book") == config_mod.current_settings().model

    async def test_it_leaves_a_snapshot_the_next_run_picks_up_from(
        self, tmp_path: Path, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = await run_book_stage(tmp_path, monkeypatch)

        assert runner.snapshot.stage == "book"
        assert (runner.ensure_notes().base_dir / "snapshot_book.json").is_file()

    async def test_the_snapshot_carries_the_order_it_laid_out(
        self, tmp_path: Path, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = await run_book_stage(tmp_path, monkeypatch)

        assert runner.snapshot.outline is not None
        assert len(runner.snapshot.outline.chapters) == 3

    async def test_the_stage_publishes_what_the_next_run_reads(
        self, tmp_path: Path, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await run_book_stage(tmp_path, monkeypatch)

        recorded = store.load("textbook").outline
        assert recorded is not None
        assert [held.ordinal for held in recorded.chapters] == [1, 2, 3]

    async def test_a_run_picking_up_after_it_starts_at_the_plan(
        self, tmp_path: Path, store: BookStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = await run_book_stage(tmp_path, monkeypatch)
        stages = runner.stages_for_run()

        assert stages[stages.index("book") + 1] == "plan"
