"""Where each part of a work stands, and what its last run was built from.

Two records, kept apart because they answer to different authorities and rot
in different ways.

**A standing is declared.** Somebody asked for this part to be revised; a run
is holding it; it is parked on a question nobody has answered; its last run
failed. None of that is derivable from the files — it is what this system was
told, and it survives everything, including a re-import that finds the work
rearranged.

**Dirtiness is derived, and never written down.** A part is out of date when
it was asked for, or when the text under it has moved since it was built, or
when the ledger holds a change it consumed and has not yet seen. Every one of
those is a question that can be asked afresh, which is exactly why none of
them is stored: a build system that persists dirtiness is a build system that
eventually believes something clean is dirty, or worse. The stamp is the only
new thing this needs — what a part was built from — and it is a record of the
past, which cannot go stale the way a prediction can.

That split is also what makes re-import safe. Reading the work again produces
a tree and nothing else; it has no opinion about state, because the only
opinions here are either declared by somebody or computed from the files as
they are now.
"""

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from hashlib import blake2b
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lup.channels.models import utc_now

from inkwell.manuscript.facts import (
    ChangeFact,
    Consumption,
    FactLedger,
    ProposedChange,
)

DIGEST_BYTES = 16
"""Length of a part's content digest.

Long enough that two different subsections never collide in a work of a few
hundred, short enough to read in a status line. Nothing cryptographic depends
on it — it answers "is this the same text as last time".
"""


def digest_of(text: str) -> str:
    """A stable fingerprint of one part's text.

    What "has this changed since it was built" is answered against. Whitespace
    is deliberately included: a heading that gained a blank line under it did
    change, and a run that reassembles a file is entitled to know.
    """
    return blake2b(text.encode("utf-8"), digest_size=DIGEST_BYTES).hexdigest()


type NodeStanding = Literal["idle", "requested", "running", "parked", "failed"]
"""What this system was told about one part, as against what it can work out.

``idle`` is the absence of any instruction and is what an unrecorded part
reads as. ``requested`` is somebody asking for a revision — the one standing
that makes a part dirty by itself. ``running`` is a lease: exactly one run
holds a part at a time. ``parked`` is waiting on an answer, and is not
failure — the work is sound and suspended. ``failed`` is a run that ended
without producing anything.
"""

ACTIVE_STANDINGS: tuple[NodeStanding, ...] = (
    "requested",
    "running",
    "parked",
    "failed",
)
"""Every standing worth reporting, which is every one but the absence of news."""


class PartText(BaseModel):
    """One part of a work paired with the text it holds at this moment.

    What a verdict is asked against. Kept as a pair rather than read inside
    the judging, because where a part's text comes from is the caller's
    business — a file on disk during a sweep, a rewrite's output before it has
    been written anywhere — and a judge that opened files itself could not be
    asked about text that is not on disk yet.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part")
    text: str = Field(description="What it holds now")
    found: bool = Field(
        default=True,
        description="Whether that text was there to read. False says the file or "
        "the heading is gone, which empty text alone cannot distinguish from a "
        "part an author emptied",
    )


LEASE_TERM = timedelta(hours=12)
"""How long a lease stands before it is worth asking whether anybody holds it.

A part run is a whole pipeline over somebody's subsection and the longest
measured one took five hours, so this is generous by a factor of two: the cost
of being wrong in one direction is a lease reported as abandoned while a run is
still working, and in the other a part that reads ``running`` for good.

Not a timeout. Nothing reclaims an expired lease, because two runs writing one
part's span is the one thing the lease exists to prevent and a process that has
gone quiet is not a process that has stopped. What expiry buys is that somebody
is *told* — the failure this closes was a part left ``running`` behind a holder
whose process was gone, with every surface reporting it as work in progress.
"""


class NodeRecord(BaseModel):
    """What this system was told about one part.

    Frozen and replaced rather than mutated, so a state passed to something
    that keeps it cannot change under that thing's feet.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part this is about")
    standing: NodeStanding = Field(
        default="idle", description="What this system was told about it"
    )
    reason: str = Field(
        default="",
        description="Why, in the words of whoever said so — an author's "
        "instruction, or what a failed run reported",
    )
    changed_at: datetime = Field(
        default_factory=utc_now, description="When the standing last moved"
    )
    holder: str = Field(
        default="",
        description="Identifier of the run holding this part, while one does",
    )

    def abandoned(self, *, term: timedelta = LEASE_TERM) -> bool:
        """Whether this part has read ``running`` for longer than a run lasts.

        Asked of the record rather than stored on it, for the reason dirtiness
        is: it is a question about now, and a flag would be a thing somebody
        has to remember to clear. A part that is not running is never
        abandoned, whatever its age — a stamp from last March is a part built
        last March, not a lease nobody honoured.
        """
        return self.standing == "running" and utc_now() - self.changed_at > term


class BuildStamp(BaseModel):
    """What one part was last built from, and how far through the ledger.

    The record every incremental build turns on. ``source_digest`` answers
    whether the text moved underneath; ``consumed`` and ``built_through``
    together answer whether anything it leaned on did. A part with no stamp
    has never been built and is dirty for that reason alone.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part this stamp is for")
    built_at: datetime = Field(
        default_factory=utc_now, description="When the run that made it finished"
    )
    source_digest: str = Field(
        description="Fingerprint of the part's text as the run left it"
    )
    consumed: Consumption = Field(
        default_factory=Consumption,
        description="What that run read of the rest of the work",
    )
    built_through: int = Field(
        default=0,
        ge=0,
        description="Ledger sequence the run saw — everything after it is news",
    )
    run: str = Field(default="", description="Identifier of the run that built it")


type Staleness = Literal[
    "fresh", "never-built", "requested", "source-gone", "source-moved", "upstream"
]
"""Why a part is or is not out of date.

Named rather than reduced to a boolean because the loop shows it and because
the repairs differ: ``source-moved`` means somebody edited the file directly,
``upstream`` means something it depends on changed, and telling an author the
difference is most of what a status display is for.

``source-gone`` is the one that is not about the prose at all: the part's text
cannot be read, because its file or its heading is no longer where the work was
imported from. It is separated from ``source-moved`` because the repair is
different in kind — a checkout to restore or an import to redo, not a rewrite —
and a work whose root has moved would otherwise report every part as edited,
which is a wrong diagnosis rather than a vague one.
"""

UNRUNNABLE_STALENESS: tuple[Staleness, ...] = ("source-gone",)
"""Verdicts no run can answer, whatever else is outstanding about the part.

A part whose text is not there has nothing for a run to revise, so scheduling
it would spend a turn to fail and would replace a diagnosis somebody can act on
— restore the checkout, or re-import — with a ``failed`` standing that reads
like the pipeline broke. Declared beside the verdicts rather than at the loop,
because the sweep reports these apart and the loop declines to schedule them,
and those two have to be the same set.
"""

STALENESS_PHRASING: dict[Staleness, str] = {
    "fresh": "up to date",
    "never-built": "never built",
    "requested": "revision asked for",
    "source-gone": "its text is no longer where the work was imported from",
    "source-moved": "its text was edited since it was built",
    "upstream": "something it leans on changed",
}
"""How each verdict reads to somebody looking at the work's state."""


class NodeVerdict(BaseModel):
    """Whether one part is out of date, and what would put it right.

    Computed on every ask and stored nowhere. ``reasons`` carries what the
    change facts that reached it said, so the run that follows can be told
    what moved rather than merely that something did.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part judged")
    staleness: Staleness = Field(description="Why it stands where it does")
    standing: NodeStanding = Field(
        default="idle", description="What this system was told about it"
    )
    reasons: tuple[str, ...] = Field(
        default=(), description="What made it stale, in the words of what moved it"
    )

    def dirty(self) -> bool:
        """Whether a run against this part has anything to do."""
        return self.staleness != "fresh"

    def render(self) -> str:
        """This verdict as a status line reads it."""
        because = f" ({'; '.join(self.reasons)})" if self.reasons else ""
        return f"{self.key}: {STALENESS_PHRASING[self.staleness]}{because}"


class WorkState(BaseModel):
    """Everything this system holds about one work, beside the work itself.

    A part nobody has said anything about and no run has built has no entry
    anywhere in here, and reads as idle and never-built. Recording only what
    happened is what keeps a re-import from having an opinion about a part it
    was never told anything about.
    """

    records: tuple[NodeRecord, ...] = Field(
        default=(), description="One entry per part something was declared about"
    )
    stamps: tuple[BuildStamp, ...] = Field(
        default=(), description="One entry per part that has been built"
    )
    ledger: FactLedger = Field(
        default_factory=FactLedger, description="Every change any run has recorded"
    )

    def record(self, key: str) -> NodeRecord | None:
        """What was declared about one part, where anything was."""
        return next((held for held in self.records if held.key == key), None)

    def standing(self, key: str) -> NodeStanding:
        """Where one part stands, idle where nothing has been said."""
        found = self.record(key)
        return found.standing if found else "idle"

    def stamp(self, key: str) -> BuildStamp | None:
        """What one part was last built from, where it has been built."""
        return next((held for held in self.stamps if held.key == key), None)

    def declared(
        self, key: str, standing: NodeStanding, reason: str, holder: str = ""
    ) -> "WorkState":
        """This state with one part's standing replaced whole."""
        kept = tuple(held for held in self.records if held.key != key)
        moved = NodeRecord(key=key, standing=standing, reason=reason, holder=holder)
        return self.model_copy(update={"records": (*kept, moved)})

    def stamped(self, stamp: BuildStamp) -> "WorkState":
        """This state with one part's build record replaced whole."""
        kept = tuple(held for held in self.stamps if held.key != stamp.key)
        return self.model_copy(update={"stamps": (*kept, stamp)})

    def active(self) -> tuple[NodeRecord, ...]:
        """Every part something is outstanding on, which a status display shows."""
        return tuple(held for held in self.records if held.standing in ACTIVE_STANDINGS)

    def abandoned(self, *, term: timedelta = LEASE_TERM) -> tuple[NodeRecord, ...]:
        """Every part held by a run that has said nothing for longer than one lasts.

        What nothing said. A run whose process ended between the pipeline
        returning and the splice left its part reading ``running``, its work
        byte-identical to what it was handed, and every surface reporting a
        rewrite in progress — for as long as anybody left it. Reported rather
        than reclaimed: whether the run is gone or merely slow is not something
        another process can tell, and taking the lease from one that is still
        writing is the one failure the lease exists to prevent.
        """
        return tuple(held for held in self.records if held.abandoned(term=term))

    def verdict(self, key: str, source: str, found: bool = True) -> NodeVerdict:
        """Whether one part is out of date, given the text it holds now.

        The order the causes are tried in is the order they are worth telling
        somebody about, and the first one that answers is the verdict: a part
        nobody has built is not usefully described as having drifted, and a
        part somebody asked for is going to be rewritten whatever else is also
        true of it. The reasons still carry everything that reached it, so a
        run is shown all of what moved rather than only the headline.

        A part whose text is not there answers before any of them, because no
        rewrite is available to it whatever else is also true: what it needs is
        the checkout it was imported from, and reporting it as edited prose
        would send somebody looking for a change nobody made.
        """
        standing = self.standing(key)
        stamp = self.stamp(key)
        reached = tuple(fact.reason() for fact in self.reaching(key))
        if not found:
            return NodeVerdict(
                key=key, staleness="source-gone", standing=standing, reasons=reached
            )
        if standing == "requested":
            asked = self.record(key)
            told = (asked.reason,) if asked and asked.reason else ()
            return NodeVerdict(
                key=key,
                staleness="requested",
                standing=standing,
                reasons=(*told, *reached),
            )
        if stamp is None:
            return NodeVerdict(key=key, staleness="never-built", standing=standing)
        if stamp.source_digest != digest_of(source):
            return NodeVerdict(
                key=key,
                staleness="source-moved",
                standing=standing,
                reasons=reached,
            )
        if reached:
            return NodeVerdict(
                key=key, staleness="upstream", standing=standing, reasons=reached
            )
        return NodeVerdict(key=key, staleness="fresh", standing=standing)

    def reaching(self, key: str) -> tuple[ChangeFact, ...]:
        """Every change this part consumed and has not been built through.

        A part's own changes are skipped: a run that redefines a term has
        already accounted for its own redefinition, and counting it would
        leave every run dirtying itself and the loop never settling.
        """
        stamp = self.stamp(key)
        if stamp is None:
            return ()
        return tuple(
            fact
            for fact in self.ledger.since(stamp.built_through)
            if fact.origin != key and stamp.consumed.touches(fact.dependency)
        )

    def built(
        self,
        key: str,
        *,
        source: str,
        consumed: Consumption,
        run: str = "",
    ) -> "WorkState":
        """This state with one part recorded as built from what it holds now.

        The stamp is taken through the ledger's current head, so anything
        recorded before this run finished is something it is presumed to have
        seen. A change another run appends after this returns is news to it,
        which is the behaviour that makes two runs of one work safe.
        """
        stamp = BuildStamp(
            key=key,
            source_digest=digest_of(source),
            consumed=consumed,
            built_through=self.ledger.head(),
            run=run,
        )
        return self.stamped(stamp).declared(key, "idle", "")

    def changed(self, origin: str, changes: Iterable[ProposedChange]) -> "WorkState":
        """This state with one run's changes appended to the ledger."""
        return self.model_copy(update={"ledger": self.ledger.appended(origin, changes)})

    def verdicts(self, held: Iterable[PartText]) -> Iterator[NodeVerdict]:
        """A verdict for each part, given the text each of them holds now."""
        for part in held:
            yield self.verdict(part.key, part.text, part.found)
