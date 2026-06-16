"""A section writer that wrote its draft must survive a late usage limit.

The writer agent persists the whole section in a single Write call, then takes
a final turn. A usage or rate limit landing on that final turn raised straight
out of ``write_section``, so the pipeline discarded a complete on-disk draft and
recorded a ``[Section failed]`` placeholder that merge then dropped. These tests
pin the recovery: the file on disk is the real output, and only a genuine
interrupt or an absent/empty draft counts as a failure.
"""

from pathlib import Path

import pytest

from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import write_section


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


async def test_keeps_on_disk_draft_when_writer_errors_after_writing(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "# Real section\n\nA complete body the writer already wrote."
    draft_path = notes.draft_path("section")
    draft_path.write_text(body, encoding="utf-8")

    async def hit_limit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Agent error: You've hit your limit")

    monkeypatch.setattr("inkwell.agent.pipeline.query", hit_limit)

    draft = await write_section("Section", notes=notes, draft_path=draft_path)

    assert draft.content == body
    assert draft.word_count == len(body.split())


async def test_propagates_when_no_draft_was_written(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hit_limit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Agent error: You've hit your limit")

    monkeypatch.setattr("inkwell.agent.pipeline.query", hit_limit)

    with pytest.raises(RuntimeError, match="hit your limit"):
        await write_section(
            "Section", notes=notes, draft_path=notes.draft_path("section")
        )


async def test_propagates_interrupt_even_with_a_draft(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_path = notes.draft_path("section")
    draft_path.write_text("# Partial", encoding="utf-8")

    async def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Command failed with exit code -2")

    monkeypatch.setattr("inkwell.agent.pipeline.query", interrupted)

    with pytest.raises(RuntimeError, match="exit code -2"):
        await write_section("Section", notes=notes, draft_path=draft_path)
