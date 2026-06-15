"""Resume must restore the snapshot's draft files to disk.

The snapshot carries section, merged, and final draft *content*, but the
``drafts/`` files are working artifacts a resumed process starts without.
Stages downstream of ``write`` read those drafts as files, so a resume that
skipped their producing stage would otherwise hand the reader a path to
nothing. These tests pin the rehydration that closes that gap.
"""

from pathlib import Path

import pytest

from inkwell.agent.models import (
    ArticlePlan,
    MergedDraft,
    SectionDraft,
    SectionPlan,
    WritingOutput,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import PipelineRunner
from inkwell.agent.tools.google_docs import truncate_tab_title


def make_plan(titles: list[str]) -> ArticlePlan:
    return ArticlePlan(
        title="T",
        thesis="t",
        target_format="blog",
        author_direction="",
        voice_notes="",
        sections=[SectionPlan(title=t, summary="s", key_points=["k"]) for t in titles],
        research_questions=[],
        source_quotes=[],
    )


@pytest.fixture
def runner(tmp_path: Path) -> PipelineRunner:
    return PipelineRunner(sources=["dummy"], notes=PipelineNotes(tmp_path / "notes"))


class TestRehydrateDraftFiles:
    def test_missing_section_drafts_are_restored(self, runner: PipelineRunner) -> None:
        runner.snapshot.section_drafts = {
            "Introduction": SectionDraft(title="Introduction", content="# Intro\n\nx"),
            "Definitions and notation": SectionDraft(
                title="Definitions and notation", content="# Defs\n\ny"
            ),
        }

        runner.rehydrate_draft_files()

        for title, draft in runner.snapshot.section_drafts.items():
            path = runner.get_draft_path(title)
            assert path.read_text(encoding="utf-8") == draft.content

    def test_merged_and_final_drafts_are_restored(self, runner: PipelineRunner) -> None:
        runner.snapshot.merged = MergedDraft(content="merged body")
        runner.snapshot.output = WritingOutput(title="T", content="final body")

        runner.rehydrate_draft_files()

        assert (
            runner.get_draft_path("merged").read_text(encoding="utf-8") == "merged body"
        )
        assert (
            runner.get_draft_path("final").read_text(encoding="utf-8") == "final body"
        )

    def test_failed_section_placeholder_is_skipped(
        self, runner: PipelineRunner
    ) -> None:
        runner.snapshot.section_drafts = {
            "Broken": SectionDraft(title="Broken", content="[Section failed: boom]"),
        }

        runner.rehydrate_draft_files()

        assert not runner.get_draft_path("Broken").exists()

    def test_existing_draft_file_is_not_clobbered(self, runner: PipelineRunner) -> None:
        path = runner.get_draft_path("Introduction")
        path.write_text("on-disk version", encoding="utf-8")
        runner.snapshot.section_drafts = {
            "Introduction": SectionDraft(
                title="Introduction", content="snapshot version"
            ),
        }

        runner.rehydrate_draft_files()

        assert path.read_text(encoding="utf-8") == "on-disk version"

    def test_empty_output_content_writes_no_file(self, runner: PipelineRunner) -> None:
        runner.snapshot.output = WritingOutput(title="T")

        runner.rehydrate_draft_files()

        assert not runner.get_draft_path("final").exists()


class TestWriteStageProducedNothing:
    """A wholly-failed write stage must be detected so the pipeline does not

    advance into merge with nothing to assemble (e.g. every section writer hit
    a usage limit and recorded a ``[Section failed: ...]`` placeholder).
    """

    def test_all_sections_failed(self, runner: PipelineRunner) -> None:
        runner.snapshot.section_drafts = {
            "A": SectionDraft(title="A", content="[Section failed: usage limit]"),
            "B": SectionDraft(title="B", content="[Section failed: usage limit]"),
        }

        assert runner.write_stage_produced_nothing() is True

    def test_one_section_usable(self, runner: PipelineRunner) -> None:
        runner.snapshot.section_drafts = {
            "A": SectionDraft(title="A", content="[Section failed: usage limit]"),
            "B": SectionDraft(title="B", content="# B\n\nreal content"),
        }

        assert runner.write_stage_produced_nothing() is False

    def test_no_sections_written_yet(self, runner: PipelineRunner) -> None:
        assert runner.write_stage_produced_nothing() is False


class TestSectionAlreadyDrafted:
    """A resumed write stage skips sections already drafted and retries the

    rest, so an interrupted run neither pays to redraft finished sections nor
    strands the ones that failed.
    """

    def test_usable_draft_is_already_drafted(self, runner: PipelineRunner) -> None:
        runner.snapshot.section_drafts = {
            "A": SectionDraft(title="A", content="# A\n\nreal content"),
        }

        assert runner.section_already_drafted("A") is True

    def test_failed_placeholder_is_not_drafted(self, runner: PipelineRunner) -> None:
        runner.snapshot.section_drafts = {
            "A": SectionDraft(title="A", content="[Section failed: usage limit]"),
        }

        assert runner.section_already_drafted("A") is False

    def test_missing_section_is_not_drafted(self, runner: PipelineRunner) -> None:
        assert runner.section_already_drafted("A") is False


class TestRehydrateSections:
    """A resume that skips plan/refine must still rebuild the section→tab map

    and the section roster from the snapshot's plan, or section writers get no
    tab to sync into and the progress badge is stuck on a polluted total.
    """

    def test_rebuilds_tab_map_and_roster_from_plan(
        self, runner: PipelineRunner
    ) -> None:
        titles = ["Introduction", "Methods", "Results"]
        runner.snapshot.plan = make_plan(titles)
        runner.known_tabs = {
            truncate_tab_title(f"§{i + 1} {t}"): f"tab{i + 1}"
            for i, t in enumerate(titles)
        }
        runner.snapshot.section_drafts = {
            "Introduction": SectionDraft(title="Introduction", content="# I\n\nx"),
        }

        runner.rehydrate_sections()

        assert [s["title"] for s in runner.state.sections] == titles
        assert runner.tab_ids == {
            "Introduction": "tab1",
            "Methods": "tab2",
            "Results": "tab3",
        }
        status = {s["title"]: s["status"] for s in runner.state.sections}
        assert status["Introduction"] == "drafted"
        assert status["Methods"] == "planned"

    def test_section_without_a_tab_still_joins_roster(
        self, runner: PipelineRunner
    ) -> None:
        runner.snapshot.plan = make_plan(["Orphan"])

        runner.rehydrate_sections()

        assert len(runner.state.sections) == 1
        assert runner.state.sections[0]["tab_id"] == ""
        assert "Orphan" not in runner.tab_ids

    def test_no_plan_is_a_noop(self, runner: PipelineRunner) -> None:
        runner.rehydrate_sections()
        assert runner.state.sections == []
