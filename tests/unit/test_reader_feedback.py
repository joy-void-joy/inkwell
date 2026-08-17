"""Reader feedback on published text reaches the stage that revises the section.

The export is keyed by chapter and section ordinal; the plan's sections carry
the same ordinal path. These tests pin the whole route: the export's actual
field set parses, prose is separated from bare votes by a declared rule, each
substantive submission lands in the file for the address it named — a section,
or the chapter where it named no section — the ones naming neither land in an
explicit unrouted file, and a stage's task text carries the path of the file
for the section it is acting on.

An export spans chapters, so the load-bearing case is a run of one chapter
reaching its own chapter's readers and no others. ``TestAMultiChapterExport``
runs that against a fixture built to the shape the author's real export was
measured to have.
"""

import json
from pathlib import Path

import pytest

from inkwell.agent.book import ChapterPlacement
from inkwell.agent.content import ContentManifest
from inkwell.agent.models import ArticlePlan, SectionPlan
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    add_reader_index_refs,
    add_reader_section_refs,
    plan_article,
    write_section,
)
from inkwell.agent.reader_feedback import (
    AddressedFile,
    ReaderFeedback,
    ReaderFeedbackTree,
    ReaderSubmission,
    SectionAddress,
    SectionAddresses,
    SubstantiveRule,
    ingest_reader_feedback,
    ordinal_prefix,
    read_export,
)

EXPORT = Path(__file__).parent / "data" / "formspree_export_sample.json"
"""A fixture derived from the author's Formspree export, one row per shape the
real file carries: bare votes, prose under each of the three prose fields, a
chapter-level row with no section ordinal, a row naming neither ordinal, and a
row that fails validation."""

CHAPTERS = Path(__file__).parent / "data" / "formspree_export_chapters.json"
"""A fixture built to the shape the author's real 574-row export was measured
to have: nine chapters recorded as 01 through 09, section ordinals that every
one of those chapters carries, chapter-level rows naming a chapter and no
section, a row naming neither, and both spellings the form records an ordinal
in — a zero-padded string and a bare integer."""


def plan_with(*titles: str) -> ArticlePlan:
    """A plan carrying nothing but the section titles a test is about."""
    return ArticlePlan(
        title="Adaptation",
        thesis="",
        target_format="blog",
        sections=[
            SectionPlan(title=title, summary="", key_points=[]) for title in titles
        ],
        research_questions=[],
        source_quotes=[],
        author_direction="",
        voice_notes="",
    )


def placed_plan(chapter: int, *titles: str) -> ArticlePlan:
    """The same plan, saying which chapter of which book it is."""
    return plan_with(*titles).model_copy(
        update={"placement": ChapterPlacement(book="adaptation", chapter=chapter)}
    )


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


@pytest.fixture
def tree(notes: PipelineNotes) -> ReaderFeedbackTree:
    return ReaderFeedbackTree(root=notes.reader_dir)


class TestParsingTheExport:
    """The export's real field set, read as typed submissions."""

    def test_reads_every_row_that_parses(self) -> None:
        parsed = read_export(EXPORT)
        assert len(parsed.submissions) == 9

    def test_reports_a_failing_row_with_its_position(self) -> None:
        parsed = read_export(EXPORT)
        assert [row.position for row in parsed.malformed] == [8]
        assert "chapter_number" in parsed.malformed[0].error

    def test_zero_padded_ordinals_read_as_numbers(self) -> None:
        padded = next(
            one
            for one in read_export(EXPORT).submissions
            if one.page_title == "Intelligence"
        )
        assert padded.address() == SectionAddress(chapter=1, section=3)

    def test_blank_section_ordinal_is_absent_not_zero(self) -> None:
        chapter_level = next(
            one
            for one in read_export(EXPORT).submissions
            if one.page_title == "Capabilities"
        )
        assert chapter_level.section_number is None
        assert chapter_level.address() is None

    def test_prose_landing_in_the_boolean_flag_is_still_prose(self) -> None:
        glitched = next(
            one
            for one in read_export(EXPORT).submissions
            if one.page_title == "Goal-Directedness"
        )
        assert [prose.field for prose in glitched.prose()] == ["has_detailed_feedback"]

    def test_ratings_are_read_as_numbers(self) -> None:
        rated = next(
            one
            for one in read_export(EXPORT).submissions
            if one.page_title == "Intelligence"
        )
        assert rated.overall_rating == 7
        assert rated.writing_clarity == 4

    def test_a_file_that_is_not_json_is_one_bad_row(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        parsed = read_export(broken)
        assert not parsed.submissions
        assert len(parsed.malformed) == 1


class TestSubstantiveRule:
    """Prose worth acting on, separated from a bare vote by a declared rule."""

    def test_a_bare_vote_carries_no_prose(self) -> None:
        vote = ReaderSubmission(feedback_type="section", vote="up", chapter_number=1)
        assert SubstantiveRule().substantive_prose(vote) == []

    def test_prose_below_the_length_default_does_not_count(self) -> None:
        terse = ReaderSubmission(feedback_type="section_targeted", comments="ok")
        assert SubstantiveRule().substantive_prose(terse) == []

    def test_the_length_is_an_overridable_default(self) -> None:
        terse = ReaderSubmission(feedback_type="section_targeted", comments="ok")
        assert SubstantiveRule(min_length=2).substantive_prose(terse)

    def test_which_fields_count_is_overridable(self) -> None:
        glitched = ReaderSubmission(
            feedback_type="section_targeted",
            has_detailed_feedback="Chapter 7 has missing spaces in places.",
        )
        assert SubstantiveRule().substantive_prose(glitched)
        assert not SubstantiveRule(prose_fields=["comments"]).substantive_prose(
            glitched
        )


class TestIdentityMapping:
    """Ordinal paths, read off the export and off the plan's own sections."""

    def test_a_titled_ordinal_addresses_its_section(self) -> None:
        assert ordinal_prefix("1.3 Intelligence") == SectionAddress(
            chapter=1, section=3
        )
        assert ordinal_prefix("01.03 Intelligence") == SectionAddress(
            chapter=1, section=3
        )

    def test_prose_is_not_an_ordinal(self) -> None:
        assert ordinal_prefix("Intelligence") is None
        assert ordinal_prefix("1.3.2 Too deep") is None
        assert ordinal_prefix("") is None

    def test_titled_sections_are_placed_by_their_own_ordinal(self) -> None:
        addresses = SectionAddresses.for_plan(
            plan_with("1.3 Intelligence", "7.3 Goals")
        )
        placed = addresses.entry_for("7.3 Goals")
        assert placed is not None
        assert (placed.chapter, placed.section) == (7, 3)

    def test_untitled_sections_fall_back_to_the_plans_ordering(self) -> None:
        addresses = SectionAddresses.for_plan(plan_with("Intelligence", "Goals"))
        placed = addresses.entry_for("Goals")
        assert placed is not None
        assert placed.section == 2
        assert placed.chapter is None

    def test_a_title_the_plan_does_not_carry_is_not_placed(self) -> None:
        addresses = SectionAddresses.for_plan(plan_with("1.3 Intelligence"))
        assert addresses.entry_for("Nowhere") is None

    def test_a_mixed_plan_places_only_its_numbered_sections(self) -> None:
        addresses = SectionAddresses.for_plan(
            plan_with("Introduction", "1.1 Foo", "1.2 Bar", "Conclusion")
        )
        assert [entry.title for entry in addresses.entries] == ["1.1 Foo", "1.2 Bar"]
        assert addresses.entry_for("Introduction") is None
        assert addresses.entry_for("Conclusion") is None


class TestChapterFromThePlansIdentity:
    """Which chapter a plan is, read off the plan rather than off its titles."""

    def test_a_placed_plan_places_its_untitled_sections_in_its_chapter(self) -> None:
        addresses = SectionAddresses.for_plan(
            placed_plan(4, "Intelligence", "Goals"),
        )
        placed = addresses.entry_for("Goals")
        assert placed is not None
        assert (placed.chapter, placed.section) == (4, 2)

    def test_an_unplaced_plan_of_plain_titles_still_knows_no_chapter(self) -> None:
        addresses = SectionAddresses.for_plan(plan_with("Intelligence", "Goals"))
        placed = addresses.entry_for("Goals")
        assert placed is not None
        assert placed.chapter is None

    def test_the_plans_identity_outranks_a_carried_over_title_prefix(self) -> None:
        addresses = SectionAddresses.for_plan(placed_plan(4, "7.3 Goals"))
        placed = addresses.entry_for("7.3 Goals")
        assert placed is not None
        assert (placed.chapter, placed.section) == (4, 3)

    def test_a_placed_mixed_plan_still_places_only_its_numbered_sections(self) -> None:
        addresses = SectionAddresses.for_plan(
            placed_plan(4, "Introduction", "4.1 Foo", "4.2 Bar", "Conclusion")
        )
        assert [entry.title for entry in addresses.entries] == ["4.1 Foo", "4.2 Bar"]
        assert addresses.entry_for("Introduction") is None

    def test_a_placed_plan_of_plain_titles_reaches_its_chapters_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        """The point of the identity: the same three plain titles route nowhere
        unplaced, because several chapters have a section 3 on file."""
        ingest_reader_feedback(tree, EXPORT)
        titles = ("Why adaptation", "What breaks", "What holds")
        placed = ReaderFeedback.for_plan(notes.reader_dir, placed_plan(1, *titles))
        unplaced = ReaderFeedback.for_plan(notes.reader_dir, plan_with(*titles))
        assert placed.for_section("What holds") == tree.section_path(
            SectionAddress(chapter=1, section=3)
        )
        assert unplaced.for_section("What holds") is None


class TestIngestion:
    """Routing the export into per-section files under the notes tree."""

    def test_counts_what_it_read_kept_and_could_not_route(
        self, tree: ReaderFeedbackTree
    ) -> None:
        report = ingest_reader_feedback(tree, EXPORT)
        assert report.read == 10
        assert report.substantive == 5
        assert report.votes == 4
        assert report.chapter_level == 1
        assert report.unrouted == 1
        assert report.sections == 3
        assert report.chapters == [1]
        assert [row.position for row in report.malformed] == [8]

    def test_writes_one_file_per_addressed_section(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        assert sorted(path.name for path in tree.sections_dir.glob("*.md")) == [
            "01.03.md",
            "01.11.md",
            "07.03.md",
        ]

    def test_a_sections_file_carries_the_readers_own_words(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        filed = tree.section_path(SectionAddress(chapter=1, section=11)).read_text(
            encoding="utf-8"
        )
        assert "only lists similarities with the brain" in filed

    def test_a_section_nobody_wrote_about_gets_no_file(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        assert not tree.section_path(SectionAddress(chapter=1, section=1)).exists()

    def test_prose_naming_a_chapter_and_no_section_lands_on_the_chapter(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        filed = tree.chapter_path(1).read_text(encoding="utf-8")
        assert "Not sure what all those icons do" in filed

    def test_prose_naming_neither_ordinal_lands_in_the_unrouted_file(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        unrouted = tree.unrouted_path.read_text(encoding="utf-8")
        assert "The site search returns nothing" in unrouted
        assert "Not sure what all those icons do" not in unrouted

    def test_the_index_lists_every_section_file_and_the_counts(
        self, tree: ReaderFeedbackTree
    ) -> None:
        report = ingest_reader_feedback(tree, EXPORT)
        index = tree.index_path.read_text(encoding="utf-8")
        assert "section 1.3" in index
        assert str(tree.section_path(SectionAddress(chapter=7, section=3))) in index
        assert report.summary() in index

    def test_the_report_is_written_beside_the_files(
        self, tree: ReaderFeedbackTree
    ) -> None:
        report = ingest_reader_feedback(tree, EXPORT)
        recorded = json.loads(tree.report_path.read_text(encoding="utf-8"))
        assert recorded["substantive"] == report.substantive

    def test_a_directory_of_exports_is_read_whole(
        self, tree: ReaderFeedbackTree, tmp_path: Path
    ) -> None:
        drop = tmp_path / "exports"
        drop.mkdir()
        for name in ("first.json", "second.json"):
            (drop / name).write_text(
                EXPORT.read_text(encoding="utf-8"), encoding="utf-8"
            )
        report = ingest_reader_feedback(tree, drop)
        assert report.read == 20
        assert len(report.sources) == 2

    def test_a_missing_source_ingests_nothing(self, tree: ReaderFeedbackTree) -> None:
        report = ingest_reader_feedback(tree, tree.root / "absent.json")
        assert report.read == 0
        assert report.sections == 0

    def test_re_ingesting_replaces_rather_than_layers(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        withdrawn = tree.section_path(SectionAddress(chapter=7, section=3))
        assert withdrawn.exists()
        assert tree.chapter_path(1).exists()

        ingest_reader_feedback(
            tree, EXPORT, rule=SubstantiveRule(prose_fields=["improvement"])
        )
        assert not withdrawn.exists()
        assert not tree.chapter_path(1).exists()
        assert not tree.unrouted_path.exists()

    def test_an_overridden_rule_changes_the_split(
        self, tree: ReaderFeedbackTree
    ) -> None:
        report = ingest_reader_feedback(
            tree, EXPORT, rule=SubstantiveRule(prose_fields=["comments"], min_length=1)
        )
        assert report.substantive == 4
        assert report.votes == 5

    def test_a_corrupt_re_export_leaves_a_good_ingestion_standing(
        self, tree: ReaderFeedbackTree, tmp_path: Path
    ) -> None:
        good = ingest_reader_feedback(tree, EXPORT)
        filed = tree.section_path(SectionAddress(chapter=7, section=3))
        assert filed.exists()

        broken = tmp_path / "truncated.json"
        broken.write_text('{"submissions": [', encoding="utf-8")
        report = ingest_reader_feedback(tree, broken)

        assert report.read == 1
        assert not report.filed
        assert filed.exists()
        assert tree.addressed_files() == [
            AddressedFile(address=address, path=tree.section_path(address))
            for address in good.addresses
        ]

    def test_a_source_that_vanished_leaves_a_good_ingestion_standing(
        self, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        filed = tree.section_path(SectionAddress(chapter=7, section=3))

        report = ingest_reader_feedback(tree, tree.root / "absent.json")

        assert report.read == 0
        assert filed.exists()


class TestScanningASource:
    """Which files in a configured directory this reader reads, and which it says
    it passed over."""

    def test_an_unreadable_suffix_is_counted_not_dropped_silently(
        self, tree: ReaderFeedbackTree, tmp_path: Path
    ) -> None:
        drop = tmp_path / "exports"
        drop.mkdir()
        (drop / "export.csv").write_text("chapter,section\n1,3\n", encoding="utf-8")

        report = ingest_reader_feedback(tree, drop)

        assert report.read == 0
        assert report.skipped == [str(drop / "export.csv")]
        assert "1 file(s) skipped" in report.summary()

    def test_the_readable_suffixes_are_an_overridable_default(
        self, tree: ReaderFeedbackTree, tmp_path: Path
    ) -> None:
        drop = tmp_path / "exports"
        drop.mkdir()
        renamed = drop / "export.formspree"
        renamed.write_text(EXPORT.read_text(encoding="utf-8"), encoding="utf-8")

        assert ingest_reader_feedback(tree, drop).read == 0

        report = ingest_reader_feedback(tree, drop, suffixes=(".formspree",))
        assert report.read == 10
        assert report.skipped == []

    def test_a_file_named_outright_is_read_whatever_it_is_called(
        self, tree: ReaderFeedbackTree, tmp_path: Path
    ) -> None:
        renamed = tmp_path / "export.txt"
        renamed.write_text(EXPORT.read_text(encoding="utf-8"), encoding="utf-8")
        assert ingest_reader_feedback(tree, renamed).read == 10


class TestReadingBySection:
    """A stage resolving its own section's file through the declared mapping."""

    def test_a_plan_section_reaches_its_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, plan_with("1.3 Intelligence", "7.3 Goals")
        )
        assert reader.for_section("1.3 Intelligence") == tree.section_path(
            SectionAddress(chapter=1, section=3)
        )

    def test_a_section_without_feedback_reaches_nothing(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        reader = ReaderFeedback.for_plan(notes.reader_dir, plan_with("2.1 Risks"))
        assert reader.for_section("2.1 Risks") is None

    def test_sections_lists_only_the_ones_with_files(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, plan_with("1.3 Intelligence", "2.1 Risks", "7.3 Goals")
        )
        assert [entry.title for entry in reader.sections()] == [
            "1.3 Intelligence",
            "7.3 Goals",
        ]

    def test_nothing_ingested_means_no_index(self, notes: PipelineNotes) -> None:
        reader = ReaderFeedback.for_plan(notes.reader_dir, None)
        assert reader.index() is None
        assert reader.unrouted() is None

    def test_an_untitled_unplaced_plan_reaches_nothing(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        """A plan that names no chapter — not in its placement, not in its
        titles — sits at no address, however few chapters filed its ordinal."""
        ingest_reader_feedback(tree, EXPORT)
        untitled = plan_with(*[f"Section {n}" for n in range(1, 12)])
        reader = ReaderFeedback.for_plan(notes.reader_dir, untitled)
        assert reader.for_section("Section 11") is None
        assert reader.for_section("Section 3") is None
        assert reader.sections() == []


class TestAMultiChapterExport:
    """One chapter's run reading an export that spans nine of them.

    The case the whole address exists for. Every section ordinal in this
    fixture is carried by every chapter, so an ordinal on its own names no
    file — what resolves a section is the chapter the plan says it is, and
    what a chapter run never reaches is a sibling chapter's readers.
    """

    def test_the_counts_reconcile_with_what_was_read(
        self, tree: ReaderFeedbackTree
    ) -> None:
        report = ingest_reader_feedback(tree, CHAPTERS)
        assert report.read == 27
        assert report.substantive == 25
        assert report.votes == 2
        assert report.chapter_level == 4
        assert report.unrouted == 1
        assert report.sections == 19
        assert report.chapters == [2, 4, 7]
        assert report.malformed == []
        assert report.substantive + report.votes == report.read

    def test_a_section_resolves_to_its_own_chapters_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, CHAPTERS)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, placed_plan(4, "Constraints", "Trade-offs", "Costs")
        )
        assert [entry.path for entry in reader.sections()] == [
            tree.section_path(SectionAddress(chapter=4, section=ordinal))
            for ordinal in (1, 2, 3)
        ]

    def test_a_sibling_chapters_readers_never_reach_this_run(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        """Nine chapters filed a section 1; only chapter 4's reaches chapter 4."""
        ingest_reader_feedback(tree, CHAPTERS)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, placed_plan(4, "Constraints", "Trade-offs", "Costs")
        )
        reached = "\n".join(
            entry.path.read_text(encoding="utf-8") for entry in reader.sections()
        )
        assert "Section 4.1 lists constraints" in reached
        assert "Section 3.1 jumps to populations" not in reached
        assert "Section 9.1 states a limit" not in reached

    def test_both_ordinal_spellings_reach_the_same_address(
        self, tree: ReaderFeedbackTree
    ) -> None:
        """A zero-padded "04"/"02" and a bare 4/2 are one section, not two."""
        ingest_reader_feedback(tree, CHAPTERS)
        filed = tree.section_path(SectionAddress(chapter=4, section=2)).read_text(
            encoding="utf-8"
        )
        assert "Section 4.2 loses me at the second worked example." in filed
        assert "Section 4.2 should name the trade-off in the heading." in filed

    def test_chapter_level_rows_reach_the_chapters_own_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, CHAPTERS)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, placed_plan(4, "Constraints")
        )
        assert reader.for_chapter() == tree.chapter_path(4)
        filed = tree.chapter_path(4).read_text(encoding="utf-8")
        assert "Chapter 4 reads like three separate essays" in filed
        assert "Chapter 4 needs a summary" in filed
        assert "Chapter 2 as a whole" not in filed

    def test_a_chapter_nobody_wrote_about_whole_gets_no_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, CHAPTERS)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, placed_plan(3, "Populations")
        )
        assert reader.for_chapter() is None

    def test_a_row_naming_neither_ordinal_is_still_unrouted(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, CHAPTERS)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, placed_plan(4, "Constraints")
        )
        assert reader.unrouted() == tree.unrouted_path
        assert "The glossary link" in tree.unrouted_path.read_text(encoding="utf-8")

    def test_a_plan_placing_no_chapter_still_reaches_nothing(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        """The deliberate nothing: every chapter carries a section 1, so an
        ordinal with no chapter would pick one of nine at random. The stage
        keeps the index and the unrouted file either way."""
        ingest_reader_feedback(tree, CHAPTERS)
        reader = ReaderFeedback.for_plan(
            notes.reader_dir, plan_with("Constraints", "Trade-offs", "Costs")
        )
        assert reader.for_section("Constraints") is None
        assert reader.for_section("Nowhere in the plan") is None
        assert reader.sections() == []
        assert reader.for_chapter() is None
        assert reader.index() == tree.index_path
        assert reader.unrouted() == tree.unrouted_path

    def test_the_plan_stage_of_a_chapter_run_lists_only_its_own(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, CHAPTERS)
        manifest = ContentManifest()
        add_reader_index_refs(
            manifest, notes, ChapterPlacement(book="adaptation", chapter=4)
        )
        assert [ref.label for ref in manifest.refs] == [
            "Reader feedback — section 4.1",
            "Reader feedback — section 4.2",
            "Reader feedback — section 4.3",
            "Reader feedback — chapter 4",
            "Reader feedback (no section)",
        ]

    def test_a_whole_draft_pass_gets_its_sections_and_its_chapter(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, CHAPTERS)
        notes.save_artifact("plan", placed_plan(4, "Constraints", "Trade-offs"))
        manifest = ContentManifest()
        add_reader_section_refs(manifest, notes)
        assert [ref.label for ref in manifest.refs] == [
            "Reader feedback — Constraints",
            "Reader feedback — Trade-offs",
            "Reader feedback — chapter 4",
            "Reader feedback (no section)",
        ]


class TestMixedTitlePlan:
    """A numbered plan with an unnumbered introduction beside its sections.

    The ordinary textbook shape, and the one that can quietly go wrong: an
    unnumbered title sits at no ordinal, so reading its position as one would
    hand its writer the readers of whichever numbered section shares that
    position. Nothing is the only correct answer.
    """

    def test_an_unnumbered_title_reaches_no_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        # "Further reading" sits third, and section 1.3 is a section readers
        # wrote about: reading that position as an ordinal would hand this
        # writer 1.3's complaints.
        mixed = plan_with("1.1 Foo", "1.2 Bar", "Further reading")
        reader = ReaderFeedback.for_plan(notes.reader_dir, mixed)
        assert reader.for_section("Further reading") is None

    def test_its_numbered_siblings_still_reach_theirs(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        mixed = plan_with("Introduction", "1.1 Foo", "1.3 Intelligence")
        reader = ReaderFeedback.for_plan(notes.reader_dir, mixed)
        assert reader.for_section("1.3 Intelligence") == tree.section_path(
            SectionAddress(chapter=1, section=3)
        )

    def test_a_whole_draft_manifest_lists_each_file_once(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        mixed = plan_with("Introduction", "1.3 Intelligence", "Conclusion")
        reader = ReaderFeedback.for_plan(notes.reader_dir, mixed)
        assert [entry.title for entry in reader.sections()] == ["1.3 Intelligence"]
        assert [entry.path for entry in reader.sections()] == [
            tree.section_path(SectionAddress(chapter=1, section=3))
        ]


class TestStageManifests:
    """What each stage's manifest lists once the export has been ingested."""

    def test_the_plan_stage_gets_the_index_and_the_unrouted_file(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        manifest = ContentManifest()
        add_reader_index_refs(manifest, notes)
        assert [ref.path for ref in manifest.refs] == [
            str(tree.index_path),
            str(tree.unrouted_path),
        ]

    def test_a_whole_draft_pass_gets_one_ref_per_section_with_feedback(
        self, notes: PipelineNotes, tree: ReaderFeedbackTree
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        notes.save_artifact("plan", plan_with("1.3 Intelligence", "2.1 Risks"))
        manifest = ContentManifest()
        add_reader_section_refs(manifest, notes)
        assert [ref.label for ref in manifest.refs] == [
            "Reader feedback — 1.3 Intelligence",
            "Reader feedback (no section)",
        ]

    def test_no_ingestion_leaves_the_manifest_untouched(
        self, notes: PipelineNotes
    ) -> None:
        notes.save_artifact("plan", plan_with("Intro"))
        manifest = ContentManifest()
        add_reader_section_refs(manifest, notes)
        assert manifest.refs == []


class TestStagePromptsCarryThePath:
    """The stage's task text names the file, so the agent reads the evidence."""

    async def test_the_section_writer_is_given_its_sections_file(
        self,
        notes: PipelineNotes,
        tree: ReaderFeedbackTree,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        section_file = tree.section_path(SectionAddress(chapter=1, section=3))
        draft_path = notes.draft_path("intelligence")
        tasks: list[str] = []

        async def capture(task: str, **_kwargs: object) -> None:
            tasks.append(task)
            draft_path.write_text("# Intelligence\n\nDrafted.", encoding="utf-8")

        monkeypatch.setattr("inkwell.agent.pipeline.query", capture)

        await write_section(
            "1.3 Intelligence",
            notes=notes,
            draft_path=draft_path,
            reader_feedback_path=section_file,
        )

        assert str(section_file) in tasks[0]
        assert "[reader_feedback]" in tasks[0]
        assert "Reader feedback" in tasks[0]

    async def test_a_section_without_feedback_gets_no_reader_lines(
        self, notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        draft_path = notes.draft_path("risks")
        tasks: list[str] = []

        async def capture(task: str, **_kwargs: object) -> None:
            tasks.append(task)
            draft_path.write_text("# Risks\n\nDrafted.", encoding="utf-8")

        monkeypatch.setattr("inkwell.agent.pipeline.query", capture)

        await write_section("2.1 Risks", notes=notes, draft_path=draft_path)

        assert "[reader_feedback]" not in tasks[0]

    async def test_the_plan_stage_is_given_the_index(
        self,
        notes: PipelineNotes,
        tree: ReaderFeedbackTree,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ingest_reader_feedback(tree, EXPORT)
        tasks: list[str] = []

        async def capture(task: str, **_kwargs: object) -> None:
            tasks.append(task)
            notes.save_artifact("plan", plan_with("1.3 Intelligence"))

        monkeypatch.setattr("inkwell.agent.pipeline.query", capture)

        await plan_article(notes)

        assert str(tree.index_path) in tasks[0]
        assert str(tree.unrouted_path) in tasks[0]
