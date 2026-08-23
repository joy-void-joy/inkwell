"""What a run read of the work around it, and what it changed in return.

This is the vocabulary propagation is keyed on, and the reason the loop
settles. A part of a book depends on the other parts in ways a filesystem
cannot see: it follows a link to another section, it uses a term another
section coined, it leans on a claim another section established. Recording
those as they happen turns "what does this depend on" from a guess into a
lookup — the same move ``BookOutline`` already makes for chapter-level
cross-references, taken down to the unit anybody actually revises.

**One shape for all three.** A link, a term, and a claim are the same kind of
edge and differ only in what they name, so they are one :class:`Dependency`
with a kind rather than three parallel lists. A fourth kind of dependence
costs a literal, not a field on every model that carries one.

**Propagation keys on the dependency, not on the node.** A run publishes the
dependencies it *changed*; a part is reached only where what it consumed and
what moved actually intersect. That is the whole of why a pass over a book
terminates: rewriting a chapter that redefines nothing and moves no heading
reaches nothing, however many parts link to it, and a cycle between two
chapters stops as soon as the rewrites stop producing changes. Keyed on nodes
instead, a book with any cycle in it would rebuild forever.

**Facts are a sequence, never a flag.** Each fact takes the next number in an
append-only ledger, and a part records the number it was last built through.
Whether it is out of date is then a question asked of the ledger rather than a
boolean somebody has to remember to clear — which is the failure every build
system that stores dirtiness eventually has.
"""

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from lup.channels.models import utc_now

type FactKind = Literal["node", "term", "claim", "finding"]
"""The four ways a part of a work can depend on something outside itself.

``node`` is structural — a link followed, or a part read whole. ``term`` is
the shared ledger: what something is called. ``claim`` is substantive: a
result or figure one part established and another leans on.

``finding`` is the one that does not come from another part. It is what the
research corpus establishes about the subject this part covers, and its
subject is the part's own key — a part depends on "what is known about what I
am about", which is a thing that changes without anybody touching the book.
Keyed that way rather than by document, because a part cannot have consumed a
document nobody had read when it last ran, and a paper published since is
precisely the news it most needs.
"""

FACT_KINDS: tuple[FactKind, ...] = get_args(FactKind.__value__)
"""Every kind there is, read off the declaration rather than repeated beside it.

What a surface offering the choice lists, so adding a fourth way to depend on
something needs nothing anywhere else.
"""

KIND_PHRASING: dict[FactKind, str] = {
    "node": "the part",
    "term": "the term",
    "claim": "the claim",
    "finding": "what the research holds on",
}
"""How each kind reads in the sentence a part is told why it went dirty."""


class Dependency(BaseModel):
    """One thing a part depends on, named the way whoever depends on it names it.

    Frozen and compared by value, so "did what this consumed intersect what
    that changed" is membership rather than a rule per kind.
    """

    model_config = ConfigDict(frozen=True)

    kind: FactKind = Field(description="Which way this part depends on that one")
    subject: str = Field(
        description="What is depended on — a node key, a term as the ledger "
        "spells it, or the identifier of a claim"
    )

    def render(self) -> str:
        """This dependency as a sentence names it."""
        return f"{KIND_PHRASING[self.kind]} {self.subject!r}"


def bears_on(key: str) -> Dependency:
    """What the corpus knows about one part's subject, as the part depends on it.

    Every part depends on this and nothing has to notice: a subsection on cyber
    risk is out of date when a paper lands that bears on cyber risk, whether or
    not its last run happened to read any research at all. Derived rather than
    recorded per run for exactly that reason — recorded, a part whose last run
    found nothing would record no dependency and never hear again.
    """
    return Dependency(kind="finding", subject=key)


class Consumption(BaseModel):
    """Everything one part's last run read of the work around it.

    Recorded by the run rather than derived afterwards, because only the run
    knows what it actually reached for: a link present in the prose that the
    writer never followed is not a dependency, and a term looked up and
    rejected is.
    """

    model_config = ConfigDict(frozen=True)

    dependencies: tuple[Dependency, ...] = Field(
        default=(), description="What this part read of the rest of the work"
    )

    def touches(self, dependency: Dependency) -> bool:
        """Whether this part read the thing ``dependency`` names."""
        return dependency in self.dependencies

    def of_kind(self, kind: FactKind) -> tuple[Dependency, ...]:
        """Everything consumed of one kind, for a stage shown its own inputs."""
        return tuple(held for held in self.dependencies if held.kind == kind)


def consumption_of(dependencies: Iterable[Dependency]) -> Consumption:
    """A consumption record with each distinct dependency held once.

    Deduplicated on the way in rather than on every comparison: a writer that
    looks a term up four times depends on it exactly as much as one that looks
    it up once, and the record is read far more often than it is written.
    """
    return Consumption(dependencies=tuple(dict.fromkeys(dependencies)))


class ProposedChange(BaseModel):
    """One change a run reports, before the ledger has given it a number.

    Numbering belongs to the ledger for the same reason a chapter's ordinal
    belongs to its outline: only the ledger knows what it has already handed
    out, and a run that numbered its own changes would collide with every
    other run of the same work.
    """

    model_config = ConfigDict(frozen=True)

    dependency: Dependency = Field(description="What this run moved")
    detail: str = Field(
        default="", description="What about it changed, in the run's own words"
    )


class ChangeFact(BaseModel):
    """One thing a run changed that some other part could have been depending on.

    ``sequence`` is what makes this a ledger entry rather than an event: a part
    records the number it was built through, so what it has yet to see is a
    range rather than a set of flags anybody has to clear.
    """

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(
        ge=1, description="This fact's place in the work's append-only ledger"
    )
    dependency: Dependency = Field(description="What moved")
    origin: str = Field(description="Key of the part whose run moved it")
    detail: str = Field(
        default="", description="What about it changed, in the run's own words"
    )
    at: datetime = Field(default_factory=utc_now, description="When it moved")

    def reason(self) -> str:
        """Why a part that consumed this is out of date, as a reader reads it."""
        because = f" — {self.detail}" if self.detail else ""
        return f"{self.origin} changed {self.dependency.render()}{because}"


class FactLedger(BaseModel):
    """Every change a work has recorded, in the order it recorded them.

    Append-only on purpose. A fact removed would leave parts that were built
    through it believing they had seen something no longer there, and a part's
    own record has no way to disagree with the ledger about what it read.
    """

    facts: tuple[ChangeFact, ...] = Field(
        default=(), description="What has changed, oldest first"
    )

    def head(self) -> int:
        """The last number handed out, zero for a work nothing has changed in."""
        return self.facts[-1].sequence if self.facts else 0

    def since(self, sequence: int) -> tuple[ChangeFact, ...]:
        """Every fact after ``sequence`` — what a part built then has left to see."""
        return tuple(fact for fact in self.facts if fact.sequence > sequence)

    def appended(self, origin: str, changes: Iterable[ProposedChange]) -> "FactLedger":
        """This ledger with one run's changes recorded, each taking the next number."""

        def numbered() -> Iterator[ChangeFact]:
            """Each change as a fact, counting on from the current head."""
            for offset, change in enumerate(changes, start=1):
                yield ChangeFact(
                    sequence=self.head() + offset,
                    dependency=change.dependency,
                    origin=origin,
                    detail=change.detail,
                )

        return FactLedger(facts=(*self.facts, *numbered()))
