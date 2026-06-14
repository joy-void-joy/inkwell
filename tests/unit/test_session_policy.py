"""Resumable-session wiring: labels route to the right resume behavior.

The pipeline persists each agent's SDK session id under its stage label and,
on resume, hands it back so the agent continues its own conversation. These
tests pin the pure logic that decides what gets resumed — label parsing,
reactive-helper exclusion, the pop-once lookup, and the filtered, persisted
capture — none of which needs a live model.
"""

from pathlib import Path

import pytest

from lup.client import stage_label

from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import PipelineRunner, is_resumable_label


@pytest.fixture
def runner(tmp_path: Path) -> PipelineRunner:
    return PipelineRunner(sources=["dummy"], notes=PipelineNotes(tmp_path / "notes"))


class TestStageLabel:
    @pytest.mark.parametrize(
        ("prefix", "label"),
        [
            ("[refine] ", "refine"),
            ("[write:Introduction] ", "write:Introduction"),
            ("[review:facts] ", "review:facts"),
            ("", None),
            ("no brackets", None),
        ],
    )
    def test_label_from_prefix(self, prefix: str, label: str | None) -> None:
        assert stage_label(prefix) == label


class TestIsResumableLabel:
    def test_pipeline_stages_resume(self) -> None:
        assert is_resumable_label("refine")
        assert is_resumable_label("write:Introduction")
        assert is_resumable_label("review:facts")

    def test_reactive_helpers_do_not_resume(self) -> None:
        assert not is_resumable_label("classify-edit:Plan")
        assert not is_resumable_label("classify-comment")
        assert not is_resumable_label("orchestrator")


class TestResumeSessionLookup:
    def test_lookup_pops_once(self, runner: PipelineRunner) -> None:
        runner.pending_resume = {"refine": "sess-1"}

        assert runner.resume_session_lookup("refine") == "sess-1"
        assert runner.resume_session_lookup("refine") is None

    def test_unknown_label_is_none(self, runner: PipelineRunner) -> None:
        assert runner.resume_session_lookup("refine") is None


class TestRecordSession:
    async def test_resumable_label_is_stored_and_persisted(
        self, runner: PipelineRunner
    ) -> None:
        await runner.record_session("write:Introduction", "sess-9")

        assert runner.snapshot.session_ids["write:Introduction"] == "sess-9"
        saved = runner.ensure_notes().base_dir / "snapshot.json"
        assert "sess-9" in saved.read_text(encoding="utf-8")

    async def test_reactive_label_is_not_stored(self, runner: PipelineRunner) -> None:
        await runner.record_session("classify-edit:Plan", "sess-9")

        assert runner.snapshot.session_ids == {}
