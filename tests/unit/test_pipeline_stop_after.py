"""The stop-after pause must fire at exactly the requested stage boundary.

``--stop-after`` runs the pipeline through a named stage, then halts cleanly so
the author can review the Doc before resuming. These tests pin the boundary
mechanism that makes the pause land on the right stage and nowhere else, the
validation that rejects a bad stop point up front, and the marker output that
lets the caller report a pause rather than a completion.
"""

from pathlib import Path

import pytest

from inkwell.agent.models import ArticlePlan
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    CHECKPOINT_STAGES,
    PipelineRunner,
    PipelineStopRequested,
    validate_checkpoint_stage,
)


@pytest.fixture
def runner(tmp_path: Path) -> PipelineRunner:
    return PipelineRunner(sources=["dummy"], notes=PipelineNotes(tmp_path / "notes"))


class TestValidateCheckpointStage:
    def test_none_and_blank_pass_through(self) -> None:
        assert validate_checkpoint_stage(None) is None
        assert validate_checkpoint_stage("   ") is None

    def test_normalizes_case_and_whitespace(self) -> None:
        assert validate_checkpoint_stage("  Plan ") == "plan"

    def test_rejects_unknown_stage(self) -> None:
        with pytest.raises(ValueError, match="Invalid stop point"):
            validate_checkpoint_stage("bogus")

    def test_rejects_non_checkpoint_stage(self) -> None:
        # resolve runs without checkpointing, so it is not a valid stop point.
        assert "resolve" not in CHECKPOINT_STAGES
        with pytest.raises(ValueError):
            validate_checkpoint_stage("resolve")

    def test_names_what_it_was_asked_about(self) -> None:
        """One rule serves the stop point, the resume point and the restart
        point, so each says which it was."""
        with pytest.raises(ValueError, match="Invalid from_stage 'bogus'"):
            validate_checkpoint_stage("bogus", what="from_stage")


class TestRunnerStopAfterValidation:
    def test_constructor_normalizes(self, tmp_path: Path) -> None:
        r = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="PLAN"
        )
        assert r.stop_after == "plan"

    def test_constructor_rejects_bad_stage(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            PipelineRunner(
                sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="nope"
            )

    def test_default_is_no_stop(self, runner: PipelineRunner) -> None:
        assert runner.stop_after is None


class TestMaybeStop:
    def test_raises_only_for_configured_stage(self, tmp_path: Path) -> None:
        r = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="plan"
        )
        r.maybe_stop("research")  # not the stop point — no raise
        r.maybe_stop("voice")
        with pytest.raises(PipelineStopRequested) as exc:
            r.maybe_stop("plan")
        assert exc.value.stage == "plan"

    def test_never_raises_without_stop_point(self, runner: PipelineRunner) -> None:
        for stage in CHECKPOINT_STAGES:
            runner.maybe_stop(stage)


class TestRunStageHonorsStopPoint:
    """run_stage is the single boundary both the fresh run and the resume loop
    funnel through, so the stop point must fire there after the stage's work."""

    async def test_stop_fires_after_matching_stage(self, tmp_path: Path) -> None:
        r = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="plan"
        )
        ran: list[str] = []

        async def fake_plan() -> None:
            ran.append("plan")

        async def noop() -> None:
            return None

        r.stage_plan = fake_plan  # type: ignore[method-assign]  # claude: ignore
        r.sync_if_requested = noop  # type: ignore[method-assign]  # claude: ignore

        with pytest.raises(PipelineStopRequested):
            await r.run_stage("plan")
        assert ran == ["plan"]  # the stage ran before the pause

    async def test_no_stop_for_other_stage(self, tmp_path: Path) -> None:
        r = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="review"
        )

        async def fake_voice() -> None:
            return None

        async def noop() -> None:
            return None

        r.stage_voice = fake_voice  # type: ignore[method-assign]  # claude: ignore
        r.sync_if_requested = noop  # type: ignore[method-assign]  # claude: ignore

        await r.run_stage("voice")  # must not raise


class TestHandlePauseOutput:
    async def test_marker_output_carries_stage_and_doc(self, tmp_path: Path) -> None:
        r = PipelineRunner(
            sources=["x"], notes=PipelineNotes(tmp_path / "n"), stop_after="plan"
        )
        r.doc_id = "doc123"
        r.doc_url = "https://docs.google.com/document/d/doc123/edit"
        r.snapshot.plan = ArticlePlan(
            title="My Title",
            thesis="t",
            target_format="blog",
            author_direction="",
            voice_notes="",
            sections=[],
            research_questions=[],
            source_quotes=[],
        )

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        r.update_overview = noop  # type: ignore[method-assign]  # claude: ignore

        output = await r.handle_pause("plan")

        assert output.paused_after == "plan"
        assert output.title == "My Title"
        assert output.google_doc_id == "doc123"
        assert "plan" in output.summary
