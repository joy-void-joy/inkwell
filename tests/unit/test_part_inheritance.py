"""What a freshly written part inherits from the text it replaces.

The pass exists because the anchoring fix removes the thing that used to keep
the author's material in the book: the standing text is no longer the piece the
run replaces, so nothing but this stops a fresh draft quietly shipping without
Figure 2.13. What is worth pinning is that the audit is *subtracted* rather than
believed, that a loss with a reason is allowed and a loss without one is not,
and that a pass told what it missed is asked again rather than accepted.
"""

from pathlib import Path

import pytest

from inkwell.manuscript.inheritance import (
    ADOPTED_FILE,
    INHERITANCE_FILE,
    Adoption,
    Audit,
    Dropped,
    InheritanceReader,
    settle,
    unaccounted,
)
from inkwell.manuscript.inventory import inventory_of

STANDING = """\
## 2.3.2 Cyber Risk

CrowdStrike's outage cost an estimated $5.4bn, per \
[Parametrix](https://parametrixinsurance.com/report).

<figure markdown="span">
![alt](Images/cJx_Image_13.png){ loading=lazy }
  <figcaption markdown="1"><b>Figure 2.13:</b> Stages of a cyberattack.</figcaption>
</figure>
"""

FRESH = """\
## 2.3.2 Cyber Risk

A sandbox escape now leads, and the outage is a supporting example.
"""


class Scripted(InheritanceReader):
    """A reader that writes what it was told to write and reports what it was
    told to report, so the loop around it is what a test is about."""

    def __init__(self, *rounds: tuple[str, tuple[Dropped, ...]]) -> None:
        self.rounds = list(rounds)
        self.tasks: list[str] = []

    async def read(self, task: str, room: Path) -> Audit | None:
        self.tasks.append(task)
        if not self.rounds:
            return None
        text, dropped = self.rounds.pop(0)
        (room / ADOPTED_FILE).write_text(text, encoding="utf-8")
        return Audit(dropped=list(dropped))


def staged(root: Path) -> tuple[Path, Path, Path]:
    """A run's room, holding the standing text and a fresh draft of it."""
    room = root / "room"
    room.mkdir(parents=True)
    standing = room / "part.md"
    standing.write_text(STANDING, encoding="utf-8")
    produced = room / "produced.md"
    produced.write_text(FRESH, encoding="utf-8")
    return standing, produced, room


class TestAnAuditIsSubtractedRatherThanBelieved:
    """A pass that dropped three citations and mentioned none of them is caught
    by arithmetic. One that said so is not caught at all, because a loss with a
    reason is a decision somebody can disagree with."""

    def test_a_loss_nobody_named_stays_unaccounted(self) -> None:
        lost = inventory_of(STANDING).lost_to(inventory_of(FRESH))

        assert not unaccounted(lost, ()).empty()

    def test_a_loss_named_by_its_figure_number_is_accounted_for(self) -> None:
        lost = inventory_of(STANDING).lost_to(inventory_of(FRESH))
        said = (Dropped(subject="Figure 2.13", reason="the claim is withdrawn"),)

        assert not unaccounted(lost, said).figures

    def test_a_loss_named_inside_a_sentence_still_counts(self) -> None:
        """The audit names things in the words a reader uses, so the URL arrives
        inside a sentence about the source rather than on its own."""
        lost = inventory_of(STANDING).lost_to(inventory_of(FRESH))
        said = (
            Dropped(
                subject="the Parametrix report at "
                "https://parametrixinsurance.com/report",
                reason="superseded",
            ),
        )

        assert not unaccounted(lost, said).citations

    def test_an_empty_subject_accounts_for_nothing(self) -> None:
        """Otherwise every unnumbered figure is accounted for by every line."""
        lost = inventory_of(STANDING).lost_to(inventory_of(FRESH))

        assert not unaccounted(lost, (Dropped(subject="", reason="x"),)).empty()


class TestAPassIsAskedAgainWithWhatItMissed:
    """A second attempt that repeated the first would get the first's answer."""

    @pytest.mark.asyncio
    async def test_the_second_attempt_is_told_exactly_what_went_missing(
        self, tmp_path: Path
    ) -> None:
        standing, produced, room = staged(tmp_path)
        reader = Scripted((FRESH, ()), (STANDING, ()))

        await settle(standing, produced, room, reader=reader)

        assert "Figure 2.13" in reader.tasks[1]
        assert "in neither the successor nor the audit" in reader.tasks[1]

    @pytest.mark.asyncio
    async def test_a_pass_that_settles_first_time_costs_one_reading(
        self, tmp_path: Path
    ) -> None:
        standing, produced, room = staged(tmp_path)
        reader = Scripted((STANDING, ()), (STANDING, ()))

        await settle(standing, produced, room, reader=reader)

        assert len(reader.tasks) == 1

    @pytest.mark.asyncio
    async def test_a_restored_second_attempt_settles(self, tmp_path: Path) -> None:
        standing, produced, room = staged(tmp_path)
        reader = Scripted((FRESH, ()), (STANDING, ()))

        adoption = await settle(standing, produced, room, reader=reader)

        assert adoption.settled()

    @pytest.mark.asyncio
    async def test_losses_that_never_get_accounted_for_do_not_settle(
        self, tmp_path: Path
    ) -> None:
        standing, produced, room = staged(tmp_path)
        reader = Scripted(*[(FRESH, ())] * 3)

        adoption = await settle(standing, produced, room, reader=reader)

        assert not adoption.settled()
        assert "Figure 2.13" in adoption.render()

    @pytest.mark.asyncio
    async def test_a_reader_that_comes_back_with_nothing_settles_nothing(
        self, tmp_path: Path
    ) -> None:
        standing, produced, room = staged(tmp_path)

        adoption = await settle(standing, produced, room, reader=Scripted())

        assert not adoption.settled()
        assert adoption.text == ""


class TestWhatWasDroppedIsKept:
    """ "Figure 2.13 was cut because the claim it illustrated is withdrawn" is
    the sentence an author wants months later, and nothing else in the run
    would hold it."""

    @pytest.mark.asyncio
    async def test_the_audit_is_written_beside_the_drafts(self, tmp_path: Path) -> None:
        standing, produced, room = staged(tmp_path)
        said = (Dropped(subject="Figure 2.13", reason="the claim is withdrawn"),)
        reader = Scripted((FRESH, said))

        await settle(standing, produced, room, reader=reader)

        kept = Adoption.model_validate_json(
            (room / INHERITANCE_FILE).read_text(encoding="utf-8")
        )
        assert kept.dropped[0].reason == "the claim is withdrawn"

    @pytest.mark.asyncio
    async def test_the_successor_is_kept_beside_the_draft_it_came_from(
        self, tmp_path: Path
    ) -> None:
        """Which of the two went into the book is unanswerable where the pass
        overwrote its own input."""
        standing, produced, room = staged(tmp_path)

        await settle(standing, produced, room, reader=Scripted((STANDING, ())))

        assert (room / ADOPTED_FILE).read_text(encoding="utf-8") == STANDING
        assert produced.read_text(encoding="utf-8") == FRESH
