"""The shared glossary, within one chapter and across a book's chapters.

A chapter is one run and a book is many, so a glossary that dies with the run
lets chapter nine rename what chapter one defined and nothing notices. These
tests pin both scopes: the per-run file a standalone article still uses,
unchanged; and the book's per-chapter partition — what crosses the chapter
boundary, what deliberately does not, which coinage wins when two chapters
raced, and what a re-run does to the terms that chapter coined before.
"""

import asyncio
import json
from pathlib import Path

import pytest
from lup.mcp import LupMcpTool, response_text

from inkwell.agent import config as config_mod
from inkwell.agent.book import BookStore, ChapterPlacement
from inkwell.agent.content import ContentManifest
from inkwell.agent.format_checks import (
    ADVISORY_PREAMBLE,
    CheckRow,
    DraftCheckReport,
    run_format_checks,
)
from inkwell.agent.models import ArticlePlan
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import PipelineRunner, add_format_check_report
from inkwell.agent.segmenter import reader
from inkwell.agent.stages import format_checks_for
from inkwell.agent.glossary import (
    BookGlossary,
    ChapterGlossary,
    GlossaryEntry,
    GlossaryScope,
    RunGlossary,
    define_term_in_glossary,
    load_chapter_glossary,
    read_glossary,
    seed_glossary,
    write_chapter_glossary,
)
from inkwell.agent.tools.stage_outputs import make_glossary_tools
from tests.unit.test_format_checks import HELD, StubJudge

RUN = "/sessions/first/pipeline_notes"
"""The run that writes a chapter the first time."""

RERUN = "/sessions/second/pipeline_notes"
"""A later, separate run of the same chapter — a different notes directory."""


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BookStore:
    """The book store every part of a run resolves, pointed at a temp root."""
    root = tmp_path / "books"
    monkeypatch.setattr(config_mod.settings, "books_path", str(root))
    return BookStore(root=root)


def chapter(store: BookStore, ordinal: int) -> BookGlossary:
    """The glossary scope a run of one chapter of the test book works in."""
    return BookGlossary(
        store=store, placement=ChapterPlacement(book="textbook", chapter=ordinal)
    )


def terms(scope: GlossaryScope) -> list[str]:
    """Every term the scope reads, in the order it reads them."""
    return [entry.term for entry in read_glossary(scope).terms]


def plan_for(placement: ChapterPlacement | None) -> ArticlePlan:
    """The plan a stage reads its placement back off, and nothing else."""
    return ArticlePlan(
        title="A chapter",
        thesis="Something follows from something else.",
        target_format="textbook",
        placement=placement,
        sections=[],
        research_questions=[],
        source_quotes=[],
        author_direction="",
        voice_notes="",
    )


def make_runner(
    tmp_path: Path, placement: ChapterPlacement | None = None
) -> PipelineRunner:
    """A runner far enough along to say where its writers' glossary lives."""
    return PipelineRunner(
        sources=["x"], notes=PipelineNotes(tmp_path / "n"), placement=placement
    )


class TestBooklessRun:
    """A piece belonging to no book keeps the per-run glossary it always had."""

    def test_seed_records_conventions(self, tmp_path: Path) -> None:
        scope = RunGlossary(path=tmp_path / "glossary.json")
        seed_glossary(scope, RUN, ["call the protocol 'the handshake'"])
        glossary = read_glossary(scope)
        assert glossary.conventions == ["call the protocol 'the handshake'"]
        assert glossary.terms == []

    def test_first_definition_wins(self, tmp_path: Path) -> None:
        scope = RunGlossary(path=tmp_path / "glossary.json")
        seed_glossary(scope, RUN, [])
        first = define_term_in_glossary(scope, "widget", "a small gadget")
        assert first.already_defined is False
        # A sibling proposing a rival meaning gets handed the canonical one.
        second = define_term_in_glossary(scope, "Widget", "something different")
        assert second.already_defined is True
        assert second.meaning == "a small gadget"

    def test_seed_preserves_coined_terms(self, tmp_path: Path) -> None:
        scope = RunGlossary(path=tmp_path / "glossary.json")
        seed_glossary(scope, RUN, ["conv a"])
        define_term_in_glossary(scope, "widget", "a gadget")
        # Re-seeding (e.g. on restart) refreshes conventions, keeps terms.
        seed_glossary(scope, RUN, ["conv a", "conv b"])
        glossary = read_glossary(scope)
        assert glossary.conventions == ["conv a", "conv b"]
        assert [t.term for t in glossary.terms] == ["widget"]

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        glossary = load_chapter_glossary(tmp_path / "absent.json")
        assert glossary.conventions == []
        assert glossary.terms == []

    def test_the_run_file_is_the_whole_of_it(self, tmp_path: Path) -> None:
        """Nothing outside the run is read or written on the bookless path."""
        path = tmp_path / "glossary.json"
        scope = RunGlossary(path=path)
        seed_glossary(scope, RUN, ["conv"])
        define_term_in_glossary(scope, "widget", "a gadget")
        assert scope.own() == path
        assert scope.read_order() == (path,)
        assert [written.name for written in tmp_path.iterdir()] == ["glossary.json"]


class TestScopeOfARun:
    """Where a run's writers coin, read off the placement the run was given."""

    def test_a_bookless_run_scopes_the_glossary_to_its_own_notes(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        runner = make_runner(tmp_path)
        notes = runner.ensure_notes()
        assert runner.glossary_scope == RunGlossary(
            path=notes.artifacts_dir / "glossary.json"
        )
        assert not store.root.exists()

    def test_a_chapter_run_scopes_the_glossary_to_its_book(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        runner = make_runner(tmp_path, ChapterPlacement(book="textbook", chapter=3))
        assert (
            runner.glossary_scope.own() == store.glossary_dir("textbook") / "003.json"
        )

    def test_the_chapter_glossary_sits_clear_of_the_chapter_records(
        self, store: BookStore
    ) -> None:
        """A glossary file must not read back as a malformed chapter record."""
        seed_glossary(chapter(store, 1), RUN, ["conv"])
        assert store.load("textbook").chapters == []


class TestAcrossChapters:
    """What one chapter settles is what the next chapter is handed."""

    def test_a_later_chapter_reads_an_earlier_chapters_terms(
        self, store: BookStore
    ) -> None:
        first = chapter(store, 1)
        seed_glossary(first, RUN, [])
        define_term_in_glossary(first, "inner alignment", "the mesa-objective gap")

        second = chapter(store, 2)
        seed_glossary(second, RERUN, [])
        assert terms(second) == ["inner alignment"]

    def test_a_rival_coinage_in_a_later_chapter_is_handed_the_earlier_one(
        self, store: BookStore
    ) -> None:
        first = chapter(store, 1)
        seed_glossary(first, RUN, [])
        define_term_in_glossary(first, "inner alignment", "the mesa-objective gap")

        second = chapter(store, 2)
        seed_glossary(second, RERUN, [])
        clash = define_term_in_glossary(
            second, "Inner Alignment", "goal misgeneralization"
        )
        assert clash.already_defined is True
        assert clash.meaning == "the mesa-objective gap"
        # Adopting a term is not coining one: chapter two records nothing.
        assert load_chapter_glossary(second.own()).terms == []

    def test_each_chapter_writes_only_its_own_file(self, store: BookStore) -> None:
        first, second = chapter(store, 1), chapter(store, 2)
        seed_glossary(first, RUN, [])
        seed_glossary(second, RUN, [])
        define_term_in_glossary(first, "widget", "a gadget")
        define_term_in_glossary(second, "gizmo", "a doohickey")

        directory = store.glossary_dir("textbook")
        assert sorted(p.name for p in directory.iterdir()) == ["001.json", "002.json"]
        assert [e.term for e in load_chapter_glossary(first.own()).terms] == ["widget"]
        assert [e.term for e in load_chapter_glossary(second.own()).terms] == ["gizmo"]

    def test_conventions_stay_the_chapters_own(self, store: BookStore) -> None:
        """A plan's conventions bind its own writers; its coinages bind the book."""
        first = chapter(store, 1)
        seed_glossary(first, RUN, ["chapter one's convention"])
        define_term_in_glossary(first, "widget", "a gadget")

        second = chapter(store, 2)
        seed_glossary(second, RUN, ["chapter two's convention"])
        glossary = read_glossary(second)
        assert glossary.conventions == ["chapter two's convention"]
        assert [e.term for e in glossary.terms] == ["widget"]

    def test_terms_are_read_in_chapter_order(self, store: BookStore) -> None:
        """Whichever chapter ran first, a reader meets them in the book's order."""
        ninth = chapter(store, 9)
        seed_glossary(ninth, RUN, [])
        define_term_in_glossary(ninth, "from nine", "coined first, read last")

        first = chapter(store, 1)
        seed_glossary(first, RERUN, [])
        define_term_in_glossary(first, "from one", "coined last, read first")

        assert terms(chapter(store, 5)) == ["from one", "from nine"]

    def test_a_raced_coinage_resolves_to_the_lower_chapter(
        self, store: BookStore
    ) -> None:
        """Two chapters running at once can each coin a term without seeing the
        other; every later reader still resolves it the same way."""
        write_chapter_glossary(
            chapter(store, 1).own(),
            ChapterGlossary(
                run=RUN, terms=[GlossaryEntry(term="widget", meaning="one's meaning")]
            ),
        )
        write_chapter_glossary(
            chapter(store, 9).own(),
            ChapterGlossary(
                run=RUN, terms=[GlossaryEntry(term="widget", meaning="nine's meaning")]
            ),
        )
        adopted = define_term_in_glossary(chapter(store, 5), "widget", "five's meaning")
        assert adopted.already_defined is True
        assert adopted.meaning == "one's meaning"


class TestRerunningAChapter:
    """A chapter owns the terms it coined, and a re-run replaces its own."""

    def test_a_re_run_retires_the_terms_that_chapter_coined(
        self, store: BookStore
    ) -> None:
        first = chapter(store, 1)
        seed_glossary(first, RUN, ["conv"])
        define_term_in_glossary(first, "widget", "a gadget")

        second = chapter(store, 2)
        seed_glossary(second, RUN, [])
        define_term_in_glossary(second, "gizmo", "a doohickey")

        seed_glossary(first, RERUN, ["conv"])
        assert load_chapter_glossary(first.own()).terms == []
        # A sibling chapter's coinages are out of reach and stay put.
        assert terms(second) == ["gizmo"]

    def test_a_re_run_coining_the_same_term_neither_duplicates_nor_orphans_it(
        self, store: BookStore
    ) -> None:
        first = chapter(store, 1)
        seed_glossary(first, RUN, [])
        define_term_in_glossary(first, "widget", "a gadget")

        seed_glossary(first, RERUN, [])
        again = define_term_in_glossary(first, "widget", "a small gadget")
        assert again.already_defined is False
        assert [e.meaning for e in read_glossary(first).terms] == ["a small gadget"]

    def test_a_resumed_run_keeps_what_it_already_coined(self, store: BookStore) -> None:
        """A re-entered write stage must not lose the terms its finished
        sections already used."""
        first = chapter(store, 1)
        seed_glossary(first, RUN, ["conv a"])
        define_term_in_glossary(first, "widget", "a gadget")

        seed_glossary(first, RUN, ["conv a", "conv b"])
        glossary = read_glossary(first)
        assert glossary.conventions == ["conv a", "conv b"]
        assert [e.term for e in glossary.terms] == ["widget"]


def lookup_tool(scope: GlossaryScope) -> LupMcpTool:
    """The tool a writer, a merge, or a rewrite calls to read the glossary."""
    return next(
        tool for tool in make_glossary_tools(scope) if tool.name == "lookup_terms"
    )


def define_tool(scope: GlossaryScope) -> LupMcpTool:
    """The tool a section writer calls to coin one."""
    return next(
        tool for tool in make_glossary_tools(scope) if tool.name == "define_term"
    )


class TestSiblingWritersAtOnce:
    """One chapter's writers run together on one loop and share one file."""

    async def test_terms_coined_at_once_all_survive(self, store: BookStore) -> None:
        """Siblings hold no lock, so a read-modify-write that yielded partway
        would have each writer overwrite the file the last one had just read."""
        scope = chapter(store, 1)
        seed_glossary(scope, RUN, [])
        define = define_tool(scope)
        await asyncio.gather(
            *(
                define.handler({"term": f"term {index}", "meaning": "coined at once"})
                for index in range(25)
            )
        )
        assert sorted(terms(scope)) == sorted(f"term {index}" for index in range(25))

    async def test_one_writer_of_a_raced_pair_coins_and_the_rest_adopt(
        self, store: BookStore
    ) -> None:
        """Writers naming the same thing at once still settle on one meaning."""
        scope = chapter(store, 1)
        seed_glossary(scope, RUN, [])
        define = define_tool(scope)
        results = [
            json.loads(response_text(response))
            for response in await asyncio.gather(
                *(
                    define.handler({"term": "widget", "meaning": f"meaning {index}"})
                    for index in range(5)
                )
            )
        ]
        canonical = read_glossary(scope).terms
        assert [entry.term for entry in canonical] == ["widget"]
        assert [result["already_defined"] for result in results].count(False) == 1
        assert [result["meaning"] for result in results] == [
            canonical[0].meaning
        ] * len(results)


class TestWhatTheStagesAreHanded:
    """The tools writing, merging and rewriting share, at the run's own scope."""

    def test_every_stage_reads_the_glossary_through_one_scope(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        """Writing, merging and rewriting are all handed this one server, so a
        merge cannot enforce a narrower glossary than the writers coined into."""
        runner = make_runner(tmp_path, ChapterPlacement(book="textbook", chapter=3))
        assert runner.glossary_server.tool_names == [
            "mcp__glossary__define_term",
            "mcp__glossary__lookup_terms",
        ]
        assert runner.glossary_scope == chapter(store, 3)

    async def test_the_glossary_tools_carry_earlier_chapters_terms(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        first = chapter(store, 1)
        seed_glossary(first, RUN, [])
        define_term_in_glossary(first, "inner alignment", "the mesa-objective gap")

        runner = make_runner(tmp_path, ChapterPlacement(book="textbook", chapter=3))
        seed_glossary(runner.glossary_scope, RERUN, ["chapter three's convention"])
        view = json.loads(
            response_text(await lookup_tool(runner.glossary_scope).handler({}))
        )
        assert [entry["term"] for entry in view["terms"]] == ["inner alignment"]
        assert view["conventions"] == ["chapter three's convention"]

    async def test_a_bookless_runs_tools_see_no_book(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        seed_glossary(chapter(store, 1), RUN, [])
        define_term_in_glossary(chapter(store, 1), "inner alignment", "not ours")

        runner = make_runner(tmp_path)
        seed_glossary(runner.glossary_scope, RERUN, [])
        view = json.loads(
            response_text(await lookup_tool(runner.glossary_scope).handler({}))
        )
        assert view["terms"] == []


DRIFTED = (
    "The thread from earlier picks back up. Goal misgeneralization is what "
    "happens when a learned objective holds on the training distribution and "
    "nowhere else. The rest of this chapter works through two cases."
)
"""A chapter two draft that renames what chapter one settled."""


def settled_in_chapter_one(store: BookStore) -> None:
    """Chapter one defines a term, and records the name it turned down."""
    first = chapter(store, 1)
    seed_glossary(first, RUN, [])
    define_term_in_glossary(
        first,
        "inner alignment",
        "the mesa-objective gap",
        ["goal misgeneralization"],
    )


def drift_row(report: DraftCheckReport) -> CheckRow:
    """The row every format carries, out of a whole format's report."""
    return next(row for row in report.rows if row.name == "terminology drift")


class TestTerminologyDriftAcrossChapters:
    """What one chapter named, and what a later chapter calls it instead.

    The drift first-definition-wins cannot reach: chapter two writes the
    rejected name into its prose, whether or not it ever called define_term,
    and no run but this one is in a position to notice.
    """

    async def test_the_row_fires_naming_the_canonical_term(
        self, store: BookStore
    ) -> None:
        settled_in_chapter_one(store)
        second = chapter(store, 2)
        seed_glossary(second, RERUN, [])

        report = await run_format_checks(
            DRIFTED,
            format_checks_for("textbook"),
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
            glossary=second,
        )
        row = drift_row(report)
        assert row.fired
        assert "inner alignment" in row.findings[0]
        assert "goal misgeneralization" in row.findings[0]

    async def test_a_chapter_that_never_coined_anything_is_still_measured(
        self, store: BookStore
    ) -> None:
        """The row reads the book, not this chapter's own coinages."""
        settled_in_chapter_one(store)
        report = await run_format_checks(
            DRIFTED,
            format_checks_for("textbook"),
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
            glossary=chapter(store, 2),
        )
        assert drift_row(report).fired

    def test_a_chapter_coining_the_rejected_name_is_handed_the_canonical_one(
        self, store: BookStore
    ) -> None:
        """A rename that goes through the tool never reaches the book at all."""
        settled_in_chapter_one(store)
        second = chapter(store, 2)
        seed_glossary(second, RERUN, [])
        renamed = define_term_in_glossary(
            second, "Goal Misgeneralization", "the same failure, renamed"
        )
        assert renamed.already_defined is True
        assert renamed.term == "inner alignment"
        assert terms(second) == ["inner alignment"]

    async def test_the_rewrite_stage_reads_the_row_for_a_chapter_run(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        """The scope reaches the check off the plan on disk, so a chapter run
        measures its book rather than reporting an empty one."""
        settled_in_chapter_one(store)
        notes = PipelineNotes(tmp_path / "n")
        notes.save_artifact(
            "plan", plan_for(ChapterPlacement(book="textbook", chapter=2))
        )
        draft_path = notes.artifacts_dir / "draft.md"
        draft_path.write_text(DRIFTED, encoding="utf-8")

        manifest = ContentManifest()
        await add_format_check_report(
            manifest,
            notes,
            DRIFTED,
            draft_path,
            target_format="textbook",
            judge=StubJudge(HELD),
        )
        listed = next(ref for ref in manifest.refs if ref.label == "Format checks")
        rendered = Path(listed.path).read_text(encoding="utf-8")
        assert "terminology drift: 1 finding(s) (advisory)" in rendered
        assert "this book calls it “inner alignment”" in rendered
        assert ADVISORY_PREAMBLE in rendered

    async def test_a_bookless_run_reports_having_measured_nothing(
        self, tmp_path: Path, store: BookStore
    ) -> None:
        """Not firing and not passing either — a piece in no book has no canon."""
        settled_in_chapter_one(store)
        notes = PipelineNotes(tmp_path / "n")
        notes.save_artifact("plan", plan_for(None))
        draft_path = notes.artifacts_dir / "draft.md"
        draft_path.write_text(DRIFTED, encoding="utf-8")

        manifest = ContentManifest()
        await add_format_check_report(
            manifest,
            notes,
            DRIFTED,
            draft_path,
            target_format="textbook",
            judge=StubJudge(HELD),
        )
        listed = next(ref for ref in manifest.refs if ref.label == "Format checks")
        rendered = Path(listed.path).read_text(encoding="utf-8")
        assert "terminology drift: ok — no book glossary" in rendered
        assert "inner alignment" not in rendered
