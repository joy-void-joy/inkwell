"""What a freshly written part inherits from the text it replaces.

A part run writes its subsection from scratch. That is the point — a run handed
the standing text as the piece it is *replacing* keeps that piece's shape,
because the sections it is shown are the sections it plans, and the most
pressing thing in the section lands fourth because a heading that was already
fourth was already there. Written from the material instead, the run answers
what the subsection has to establish rather than what the last draft happened
to establish first.

That trade has one cost, and this is where it is paid. The standing text is
somebody's book: it carries figures the work numbers, citations somebody
chased, and claims nobody restates for free. A draft written from scratch keeps
what it happened to keep. So the fresh draft is not what goes into the work —
what goes in is what this pass settles, reading the two against each other and
deciding what the successor inherits.

**Conservative on deletion, and checked rather than trusted.** Every figure and
every citation the standing text carried either survives into the adopted text
or is named as dropped with a reason. That is the same bias merge-conflict
resolution runs on, and for the same reason: a rename on one side must not
swallow an addition on the other. What makes it bind is :mod:`.inventory` — the
audit is subtracted from what the two texts actually carry, so a pass that
dropped three citations and mentioned none of them is caught by arithmetic
instead of believed. A pass that comes back with losses it did not account for
is told exactly which, and asked again.

**A claim is not counted, and is asked for anyway.** Figures and citations are
countable and so they are counted; a claim is not, and no check here can say
whether one survived. What this pass can do is put the question in front of a
reader who has both texts open, which is the only place it is answerable at
all.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.client import query
from inkwell.agent.config import stage_model
from inkwell.manuscript.inventory import PartInventory, inventory_of

logger = logging.getLogger(__name__)

ADOPTED_FILE = "adopted.md"
"""What the text this pass settled is called, in the run's own room.

Beside the draft it was settled from rather than over the top of it. Which of
the two went into the book is a question somebody asks after a bad pass, and it
is unanswerable where the pass overwrote its own input.
"""

INHERITANCE_FILE = "inherited.json"
"""Where the audit is kept: what was dropped, and why each one was.

A record rather than a report. "Figure 2.13 was cut because the claim it
illustrated is withdrawn" is the sentence an author wants months later, and
nothing else in the run would hold it.
"""

INHERITANCE_STAGE = "review"
"""Which stage's model tier this runs at.

A reviewer, and billed as one. It is reading two full drafts of somebody's
subsection against each other and deciding what the book keeps, which is a
judgement of the same kind the reviewers make and not one to buy cheaply.
"""

INHERITANCE_ATTEMPTS = 3
"""How many times a pass is asked before its losses stand as unaccounted.

More than one because the repair is easy and specific — "you dropped these
three, restore them or say why" is a question with an answer — and bounded
because a pass that has not settled it by the third try is not going to.
"""

INHERITANCE_PROMPT = """\
You are settling what one subsection of a book keeps.

You have two texts. One is the subsection as the book holds it now — the \
author's, published, carrying figures the book numbers and citations somebody \
chased down. The other is a fresh draft of the same subsection, written from \
the material rather than from the standing text, so its shape is its own and \
it is current where the standing text has dated.

Neither is the answer. Write the one that is: the successor this subsection \
should have, taking the fresh draft's structure and currency and the standing \
text's material wherever the standing text still has the better of it.

**Deletion is the thing to be careful about.** Anything the standing text \
carried and the successor does not is a loss, and losses here are silent — \
nobody diffs a rewritten subsection. So every figure and every citation in the \
standing text either appears in what you write or is reported as dropped with \
a reason. The same goes for its claims, which nothing can count for you: a \
number, a case, a caveat, or a named result that the standing text established \
and your successor does not is either restored or reported.

A stated reason is enough. "Superseded by a better source", "the claim is \
withdrawn — the attribution was wrong", "folded into the paragraph above" are \
all fine. What is not available is silence.

**Figures are constraints.** Reproduce each figure block exactly as the \
standing text spells it — the same number, the same image path, the same \
caption — placed wherever your successor's argument wants it. Never renumber \
one, never rewrite a caption to match new prose, and never compose a new \
figure around an image path.

**Open with the subsection's own heading**, exactly as the standing text \
spells it. The successor is spliced into the file at that heading, and one \
that opens with anything else has nowhere to go.
"""


UNEXPLAINED = (
    "the standing text carried this and the successor does not; the run gave "
    "no reason, so nobody has decided it should go"
)
"""The reason recorded where the audit found a loss and named no cause for it."""


class Dropped(BaseModel):
    """One thing the standing text carried that the successor does not.

    ``subject`` is what was dropped in the words the text names it by — the
    figure's number, the citation's URL, a phrase carrying the claim — because
    the audit is matched back against what the two texts actually hold, and a
    subject that named nothing findable would account for nothing.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(
        description="What was dropped, named as the standing text names it — a "
        "figure number, a cited URL, or a phrase carrying the claim"
    )
    reason: str = Field(description="Why the successor does without it")

    def render(self) -> str:
        """This drop as a line of the record."""
        return f"{self.subject} — {self.reason}"


class Audit(BaseModel):
    """What one inheritance pass says it left behind."""

    dropped: list[Dropped] = Field(
        default=[],
        description="Everything the standing text carried and the "
        "successor does not, each with the reason it does without it",
    )
    note: str = Field(
        default="", description="What this pass did to the two texts, in a sentence"
    )


class Adoption(BaseModel):
    """The text a part run puts into the work, and what settling it cost.

    Returned rather than written, for the reason every other part of this
    system returns rather than writes: the loop decides what happens to a run
    that could not settle, and it needs the whole of what happened in one
    value.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(default="", description="The successor, as it goes into the work")
    dropped: tuple[Dropped, ...] = Field(
        default=(), description="What was dropped, and why each one was"
    )
    unaccounted: PartInventory = Field(
        default_factory=PartInventory,
        description="What the standing text carried that neither survived nor "
        "was named as dropped",
    )
    failure: str = Field(
        default="", description="Why nothing was settled, where nothing was"
    )

    def settled(self) -> bool:
        """Whether there is a successor here that accounted for everything."""
        return bool(self.text) and self.unaccounted.empty() and not self.failure

    def adoptable(self) -> bool:
        """Whether there is a successor here the work can take at all.

        Weaker than :meth:`settled` on purpose. A rewrite that lost something
        nobody explained is still this part rewritten, and refusing it threw
        away the whole run — its brief, its research and its drafting — over a
        citation the audit could not match back. What went unaccounted is
        recorded as a drop with no reason given, which is the honest name for
        it, and said to whoever is watching the run.
        """
        return bool(self.text) and not self.failure

    def unexplained(self) -> tuple[Dropped, ...]:
        """Each unaccounted loss, in the same shape a deliberate drop takes.

        Filed beside the drops the run gave reasons for rather than in a list
        of its own, so whoever reads what a run did sees the whole of what the
        part stopped carrying without having to know which list to look in.
        """

        def each() -> Iterator[Dropped]:
            for figure in self.unaccounted.figures:
                yield Dropped(subject=figure.render(), reason=UNEXPLAINED)
            for citation in self.unaccounted.citations:
                yield Dropped(subject=citation, reason=UNEXPLAINED)

        return tuple(each())

    def render(self) -> str:
        """Why this adoption did not settle, as the part's failure reads it."""
        if self.failure:
            return self.failure
        lost = self.unaccounted
        counted = ", ".join(
            said
            for said in (
                f"{len(lost.figures)} figure(s)" if lost.figures else "",
                f"{len(lost.citations)} citation(s)" if lost.citations else "",
            )
            if said
        )
        return (
            f"the successor dropped {counted} the standing text carried, and "
            f"accounted for none of them:\n{lost.render()}"
        )


def accounts_for(dropped: Dropped, subject: str) -> bool:
    """Whether one reported drop is about the thing ``subject`` names.

    Containment rather than equality, because the audit names things in the
    words a reader uses: "Figure 2.13" arrives inside "Figure 2.13, whose claim
    is withdrawn", and a URL arrives inside a sentence about the source. A
    subject that is the empty string matches nothing, which is what keeps an
    unnumbered figure from being accounted for by every line of the audit.
    """
    return bool(subject) and subject in dropped.subject


def unaccounted(lost: PartInventory, audit: Iterable[Dropped]) -> PartInventory:
    """What was lost and not named, which is the only kind of loss that is a fault.

    A loss with a reason is a decision somebody can disagree with; a loss
    without one is a decision nobody made. Only the second is worth stopping a
    run over, which is why the audit subtracts rather than merely accompanies.
    """
    reported = list(audit)

    def named(*subjects: str) -> bool:
        """Whether any line of the audit is about any of these names."""
        return any(
            accounts_for(one, subject) for one in reported for subject in subjects
        )

    return PartInventory(
        figures=tuple(one for one in lost.figures if not named(one.number, one.image)),
        citations=tuple(one for one in lost.citations if not named(one)),
        words=lost.words,
    )


def asked(standing: Path, produced: Path, adopted: Path, missing: PartInventory) -> str:
    """What one attempt is asked, which is not what the attempt before it was.

    A second attempt that repeated the first would get the first's answer. What
    it is given instead is the arithmetic: these exact things were in the
    standing text, are in neither your successor nor your audit, and one of
    those two has to change.
    """
    task = (
        f"The subsection as the book holds it: {standing}\n"
        f"The fresh draft of it: {produced}\n\n"
        f"Write the successor to {adopted}, and report what you dropped."
    )
    if missing.empty():
        return task
    return (
        f"{task}\n\nYour last attempt left these in neither the successor nor "
        f"the audit. Each one was in the standing text and is now nowhere:\n\n"
        f"{missing.render()}\n\n"
        f"Restore each into the successor, or report it as dropped with a reason."
    )


class InheritanceReader(ABC):
    """Who reads the two drafts and writes the successor.

    A seam, only ever injected: nothing here constructs one for itself, so a
    test hands over a reader that copies the fixture and a run hands over one
    that spends a model. Asked for one attempt rather than for the settled
    answer, because the loop around the attempts — subtract what was lost,
    name what the audit missed, ask again — is this module's and not a
    reader's to reimplement.
    """

    @abstractmethod
    async def read(self, task: str, room: Path) -> Audit | None:
        """Write the successor into ``room``, and say what it left behind."""


class ModelInheritance(InheritanceReader):
    """One reading of both drafts, bought from a model.

    ``model`` empty resolves the tier at call time rather than at construction,
    so a reader built once still follows the settings a session later selects.
    """

    def __init__(self, model: str = "") -> None:
        self.model = model

    async def read(self, task: str, room: Path) -> Audit | None:
        return await query(
            task,
            output_type=Audit,
            model=self.model or stage_model(INHERITANCE_STAGE),
            system_prompt=INHERITANCE_PROMPT,
            tools=["Read", "Write", "Edit"],
            autonomy="unattended",
            prefix="inherit",
            cwd=room,
        )


async def settle(
    standing: Path,
    produced: Path,
    room: Path,
    *,
    reader: InheritanceReader | None = None,
    attempts: int = INHERITANCE_ATTEMPTS,
) -> Adoption:
    """Read the fresh draft against the standing text and settle the successor.

    Asked again where it comes back having lost something it did not mention,
    told exactly what. The loop ends on the first attempt that accounts for
    everything, so a pass that gets it right first time costs one reading.
    """
    held = inventory_of(standing.read_text(encoding="utf-8"))
    reading = reader if reader is not None else ModelInheritance()
    adopted = room / ADOPTED_FILE
    missing = PartInventory()
    settling = Adoption(failure="the inheritance pass produced nothing")

    for attempt in range(1, attempts + 1):
        audit = await reading.read(asked(standing, produced, adopted, missing), room)
        if audit is None or not adopted.is_file():
            logger.warning(
                "Inheritance attempt %d for %s settled nothing", attempt, room
            )
            continue
        text = adopted.read_text(encoding="utf-8")
        missing = unaccounted(held.lost_to(inventory_of(text)), audit.dropped)
        settling = Adoption(
            text=text, dropped=tuple(audit.dropped), unaccounted=missing
        )
        logger.info(
            "Inheritance attempt %d: %d dropped with a reason, %d unaccounted",
            attempt,
            len(audit.dropped),
            len(missing.figures) + len(missing.citations),
        )
        if settling.settled():
            break

    (room / INHERITANCE_FILE).write_text(
        settling.model_dump_json(indent=2), encoding="utf-8"
    )
    return settling


def recorded(adoption: Adoption) -> Iterator[str]:
    """Each drop as a line for whoever reads what a run did."""
    for one in adoption.dropped:
        yield one.render()
