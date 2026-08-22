"""The pass that reads a wave of rewrites against each other.

The link step. Every other reviewer in this system reads one piece and asks
whether it is good; none of them can see the failure a book has and an article
does not — two parts, revised in the same wave by different runs, that are each
sound and that disagree. Chapter four now defines a term chapter two defines
differently. Chapter seven's summary describes a section that was rewritten out
from under it. Nothing in a per-part review can catch either, because neither
part is wrong on its own.

**It runs after a wave, not after a part.** A reconciler that ran per part
would compare a rewrite against parts that are themselves about to change, and
would file findings that the next rewrite invalidates. A wave is the unit
where the question is answerable.

**Its findings are change facts, not comments.** What it produces goes back
into the ledger, so a part it names is dirtied through exactly the mechanism
everything else uses, and the loop that follows picks it up with a reason
saying what disagreed with what. A reconciler whose output was advice would
need somebody to read the advice; one whose output is a change fact needs
nothing.

**Only what actually ran is compared.** Reading a whole book to reconcile two
rewritten subsections would cost the book on every wave and find, almost
always, that the parts nobody touched still agree with each other.
"""

import logging
from collections.abc import Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.client import query
from inkwell.agent.config import stage_model
from inkwell.manuscript.facts import Dependency, ProposedChange
from inkwell.manuscript.graph import PartReading
from inkwell.manuscript.tree import Manuscript

logger = logging.getLogger(__name__)

RECONCILE_STAGE = "review"
"""Which stage's model tier this runs at.

A reviewer, and billed as one. It is reading several parts of somebody's book
against each other, which is the hardest reading anything here does, so it
inherits the reviewers' tier rather than declaring a cheaper one.
"""

RECONCILE_PROMPT = """\
You are reading several parts of one book that were just revised, separately \
and at the same time, by different runs. Each was reviewed on its own and each \
is sound on its own. Your only question is whether they still agree with each \
other, and with the parts of the book quoted around them.

Report a finding only where two parts genuinely conflict — the same term used \
for two things or two terms for one thing, a claim one part now makes that \
another contradicts, a summary or forward reference that describes a part as \
it no longer is, an argument one part now assumes that the part it depends on \
no longer establishes.

Do not report that a part could be better, could be clearer, or repeats \
something. Repetition across a textbook is often deliberate. You are looking \
for disagreement, and nothing else.

For each conflict, name the two parts by key, say what disagrees, and say \
which of the two you believe should change and why.
"""


class Conflict(BaseModel):
    """One disagreement between two parts of a work.

    Two keys rather than a sentence naming them, because what happens next is
    a lookup: the part that should change is dirtied, and it is dirtied by
    key. A sentence would have to be parsed back apart to do it.
    """

    model_config = ConfigDict(frozen=True)

    between: str = Field(description="Key of one part in the disagreement")
    and_part: str = Field(description="Key of the other")
    subject: str = Field(
        description="What they disagree about — the term, claim, or reference"
    )
    kind: str = Field(
        default="claim", description="Whether the subject is a term or a claim"
    )
    detail: str = Field(description="What the disagreement is")
    should_change: str = Field(
        default="",
        description="Key of the part the reconciler judged should give way",
    )

    def changes(self) -> tuple[ProposedChange, ...]:
        """This conflict as the change facts that reach the part that must move.

        Published in the name of the part that stays put, addressed at what
        the two disagree about, so the part that gives way is reached through
        the ordinary dependency intersection and told what it is being asked
        to reconcile with.
        """
        origin = self.and_part if self.should_change == self.between else self.between
        kind = "term" if self.kind == "term" else "claim"
        return (
            ProposedChange(
                dependency=Dependency(kind=kind, subject=self.subject),
                detail=f"reconciliation: {self.detail}",
            ),
            ProposedChange(
                dependency=Dependency(kind="node", subject=origin),
                detail=f"reconciliation: {self.detail}",
            ),
        )


class Reconciliation(BaseModel):
    """What one reconciliation pass found across a wave of rewrites."""

    conflicts: list[Conflict] = Field(
        default=[], description="Every disagreement between the parts read"
    )


def quoted(manuscript: Manuscript, readings: Iterable[PartReading]) -> str:
    """The parts a reconciler reads, each named by the key it will report.

    Named by key rather than by title because the finding has to come back
    addressed to something the state can be asked about, and a title is not
    that. The path down the work is given beside it so the reconciler knows
    which parts are neighbours and which are chapters apart.
    """

    def rendered() -> Iterator[str]:
        """Each part as a block the reconciler can quote back by key."""
        for reading in readings:
            node = reading.node
            yield f"### {node.key} — {node.title}\n\n{reading.text}"

    return f"# {manuscript.title}\n\n" + "\n\n".join(rendered())


async def reconcile(
    manuscript: Manuscript, readings: Iterable[PartReading]
) -> Reconciliation:
    """Read a wave of rewritten parts against each other.

    A wave of one is not reconciled: a part cannot disagree with itself, and
    spending a reviewer to establish that is the kind of cost that makes a
    loop too expensive to leave running.
    """
    held = list(readings)
    if len(held) < 2:
        return Reconciliation()

    answered = await query(
        quoted(manuscript, held),
        output_type=Reconciliation,
        model=stage_model(RECONCILE_STAGE),
        system_prompt=RECONCILE_PROMPT,
        prefix="reconcile",
    )
    if answered is None:
        logger.warning("The reconciler returned nothing for %s", manuscript.title)
        return Reconciliation()
    found = answered
    logger.info(
        "Reconciled %d part(s) of %s: %d conflict(s)",
        len(held),
        manuscript.title,
        len(found.conflicts),
    )
    return found


def reconciled_changes(found: Reconciliation) -> Iterator[ProposedChange]:
    """Every change fact a reconciliation produces, ready for the ledger."""
    for conflict in found.conflicts:
        yield from conflict.changes()
