"""Tests for PipelineNotes — file-backed shared knowledge base."""

import pytest

from inkwell.agent.models import ClassifiedComment
from inkwell.agent.notes import PipelineNotes


@pytest.fixture
def notes(tmp_path):
    return PipelineNotes(tmp_path / "notes")


class TestPipelineNotesComments:
    """Comment add/list/filter operations."""

    @pytest.mark.anyio
    async def test_add_and_list(self, notes: PipelineNotes) -> None:
        comment = ClassifiedComment(
            comment_id="c1",
            content="Fix the intro",
            impact="stage_local",
            tags=["tone"],
        )
        await notes.add_comment(comment)
        result = await notes.list_comments()
        assert len(result) == 1
        assert result[0].comment_id == "c1"

    @pytest.mark.anyio
    async def test_filter_by_impact(self, notes: PipelineNotes) -> None:
        await notes.add_comment(
            ClassifiedComment(comment_id="c1", content="minor", impact="clarification")
        )
        await notes.add_comment(
            ClassifiedComment(
                comment_id="c2", content="critical", impact="plan_breaking"
            )
        )
        plan_breaking = await notes.list_comments(impact="plan_breaking")
        assert len(plan_breaking) == 1
        assert plan_breaking[0].comment_id == "c2"

    @pytest.mark.anyio
    async def test_has_plan_breaking(self, notes: PipelineNotes) -> None:
        assert not await notes.has_plan_breaking()
        await notes.add_comment(
            ClassifiedComment(comment_id="c1", content="wrong", impact="plan_breaking")
        )
        assert await notes.has_plan_breaking()

    @pytest.mark.anyio
    async def test_empty_list(self, notes: PipelineNotes) -> None:
        result = await notes.list_comments()
        assert result == []


class TestPlanBreakingLifecycle:
    """Clearing and downgrading after a restart consumes plan-breaking feedback."""

    @pytest.mark.anyio
    async def test_clear_stops_retriggering(self, notes: PipelineNotes) -> None:
        await notes.add_comment(
            ClassifiedComment(
                comment_id="c1", content="wrong angle", impact="plan_breaking"
            )
        )
        await notes.clear_plan_breaking()
        assert not await notes.has_plan_breaking()

    @pytest.mark.anyio
    async def test_downgrade_keeps_feedback_but_stops_retriggering(
        self, notes: PipelineNotes
    ) -> None:
        await notes.add_comment(
            ClassifiedComment(
                comment_id="c1", content="wrong angle", impact="plan_breaking"
            )
        )
        await notes.downgrade_plan_breaking()

        assert not await notes.has_plan_breaking()
        feedback = await notes.get_all_feedback()
        assert "wrong angle" in feedback
        assert "Critical Author Feedback" not in feedback
        assert "Author Direction" in feedback


class TestPipelineNotesTerminal:
    """Terminal input handling."""

    @pytest.mark.anyio
    async def test_add_and_list_terminal(self, notes: PipelineNotes) -> None:
        await notes.add_terminal_input("emphasize the conclusion")
        result = await notes.list_terminal_inputs()
        assert len(result) == 1
        assert result[0].content == "emphasize the conclusion"
        assert result[0].impact == "stage_local"

    @pytest.mark.anyio
    async def test_multiple_terminal_inputs_ordered(self, notes: PipelineNotes) -> None:
        await notes.add_terminal_input("first")
        await notes.add_terminal_input("second")
        result = await notes.list_terminal_inputs()
        assert [r.content for r in result] == ["first", "second"]


class TestPipelineNotesResearch:
    """Research note operations."""

    @pytest.mark.anyio
    async def test_add_research_note(self, notes: PipelineNotes) -> None:
        await notes.add_research_note("climate-data", "CO2 levels rising")
        path = notes.research_dir / "climate-data.md"
        assert path.exists()
        assert path.read_text() == "CO2 levels rising"


class TestGetAllFeedback:
    """Rendered markdown output from get_all_feedback."""

    @pytest.mark.anyio
    async def test_empty_returns_empty(self, notes: PipelineNotes) -> None:
        result = await notes.get_all_feedback()
        assert result == ""

    @pytest.mark.anyio
    async def test_includes_all_categories(self, notes: PipelineNotes) -> None:
        await notes.add_comment(
            ClassifiedComment(
                comment_id="c1", content="wrong angle", impact="plan_breaking"
            )
        )
        await notes.add_comment(
            ClassifiedComment(
                comment_id="c2", content="more detail", impact="stage_local"
            )
        )
        await notes.add_terminal_input("add charts")
        await notes.add_research_note("key", "some data")

        result = await notes.get_all_feedback()
        assert "Critical Author Feedback" in result
        assert "Author Direction" in result
        assert "Terminal Input" in result
        assert "Research Notes" in result
        assert "wrong angle" in result
        assert "add charts" in result


class TestReaderDirectory:
    """Where reader feedback on published text is filed.

    Section-scoped feedback is addressed by ordinal path rather than matched
    against comment text; ``tests/unit/test_reader_feedback.py`` covers the
    route end to end.
    """

    def test_the_reader_directory_is_created(self, notes: PipelineNotes) -> None:
        assert notes.reader_dir.is_dir()


class TestPipelineNotesBrief:
    """The author's brief — persisted so every deciding stage can read it."""

    def test_brief_round_trips(self, notes: PipelineNotes) -> None:
        notes.save_brief("Self-contained; do not cite the source.")
        assert notes.load_brief() == "Self-contained; do not cite the source."

    def test_missing_brief_is_empty(self, notes: PipelineNotes) -> None:
        assert notes.load_brief() == ""
