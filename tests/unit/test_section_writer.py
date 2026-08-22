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

from lup.actors.cohort import ActorCohort
from lup.actors.refs import ActorRef

from inkwell.agent.cohort import run_cohort
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import write_section


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


@pytest.fixture
def cohort(tmp_path: Path) -> ActorCohort:
    """A real population, so what a raise does to the roster is exercised too."""
    return run_cohort(tmp_path / "artifacts")


@pytest.fixture
def writer(cohort: ActorCohort) -> ActorRef:
    return cohort.actor("writer", "section")


def raising(error: Exception, cohort: ActorCohort, monkeypatch: pytest.MonkeyPatch):
    """Make this cohort's turn raise, recording what a real round would.

    A round announces the address before it takes the turn and, where the
    failure settles the agent, records it against that address. Both are what
    the writer's own verdict afterwards has to answer, so a stub that skipped
    them would leave nothing for the test to be about.
    """

    async def hit(actor: ActorRef, *_args: object, **_kwargs: object) -> None:
        cohort.spawn(actor, "write")
        if cohort.settles(error):
            await cohort.finish(actor, error=str(error))
        raise error

    monkeypatch.setattr(cohort, "round", hit)


async def test_keeps_on_disk_draft_when_writer_errors_after_writing(
    notes: PipelineNotes,
    cohort: ActorCohort,
    writer: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Real section\n\nA complete body the writer already wrote."
    draft_path = notes.draft_path("section")
    draft_path.write_text(body, encoding="utf-8")
    raising(RuntimeError("Agent error: You've hit your limit"), cohort, monkeypatch)

    draft = await write_section(
        "Section", notes=notes, draft_path=draft_path, cohort=cohort, actor=writer
    )

    assert draft.content == body
    assert draft.word_count == len(body.split())


async def test_a_writer_that_produced_its_draft_is_recorded_as_finishing(
    notes: PipelineNotes,
    cohort: ActorCohort,
    writer: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The roster has to agree with the file, or every door reads a failure.

    The turn raised, so the round recorded the error against this address.
    Only the writer knows the section was nonetheless written, which is why
    it says so afterwards rather than leaving the round's verdict standing.
    """
    draft_path = notes.draft_path("section")
    draft_path.write_text("# Real section\n\nTwo words.", encoding="utf-8")
    raising(RuntimeError("Agent error: You've hit your limit"), cohort, monkeypatch)

    await write_section(
        "Section", notes=notes, draft_path=draft_path, cohort=cohort, actor=writer
    )

    spawned = [entry for entry in cohort.live() if entry.address == writer.label()]
    assert [entry.error for entry in spawned] == [""]


async def test_propagates_when_no_draft_was_written(
    notes: PipelineNotes,
    cohort: ActorCohort,
    writer: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raising(RuntimeError("Agent error: You've hit your limit"), cohort, monkeypatch)

    with pytest.raises(RuntimeError, match="hit your limit"):
        await write_section(
            "Section",
            notes=notes,
            draft_path=notes.draft_path("section"),
            cohort=cohort,
            actor=writer,
        )


async def test_propagates_interrupt_even_with_a_draft(
    notes: PipelineNotes,
    cohort: ActorCohort,
    writer: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_path = notes.draft_path("section")
    draft_path.write_text("# Partial", encoding="utf-8")
    raising(RuntimeError("Command failed with exit code -2"), cohort, monkeypatch)

    with pytest.raises(RuntimeError, match="exit code -2"):
        await write_section(
            "Section",
            notes=notes,
            draft_path=draft_path,
            cohort=cohort,
            actor=writer,
        )
