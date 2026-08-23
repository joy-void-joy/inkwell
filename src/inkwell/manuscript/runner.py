"""One part of a work, taken through the writing pipeline and put back.

The build system's compile step. Everything above this decides *which* part to
run and *why*; this runs it, returns its prose to the file it came from, and
records what the run read and what it moved so the parts leaning on it hear.

**A part run is a revision, not a fresh piece.** The part's own text is the
run's material, under ``revision_target`` — the pipeline path that already
exists for exactly this. Nothing here re-implements a stage; a part is one
ordinary run whose source happens to be a span of somebody's book.

**Composed at the outside, deliberately.** The pipeline is not told it is
running inside a work. It is handed material, and what comes back is spliced
into place by this module, which is also the only thing that touches the
work's state. That keeps the loop's concurrency story simple — the loop is the
single writer of state, a run writes one span of one file — and it keeps a
book-length feature out of a pipeline that already runs to a quarter of a
megabyte. What the run is told is nonetheless enough to reach what it shares
with its siblings: the work's term ledger is handed in as the scope its writers
coin into, so the authors' declared vocabulary answers a writer's lookup during
the run rather than being reconciled against its prose afterwards, when the
name is already on the page.

**The work is readable, not merely referred to.** A part told it sits among
others and asked not to repeat them has been given a rule about text it cannot
see, and what that produces is a run which finds the work where it *can* reach
it — the published edition, a different version of the book it is holding one
page of. So the instruction names the work's root and the files either side of
this part, and the run reads them.

**The format comes from the work, not from the part.** A run handed one
subsection and asked to infer its own format is guessing at a book it can see a
page of, and two parts of one work guessing differently is how a textbook
acquires a chapter that reads like a blog post. The work declares it once at
import and every part inherits it.

**The lease is the standing.** A part is marked ``running`` with the session
holding it before anything starts, and a part already held is one the loop
will not schedule twice. A run that fails leaves ``failed`` with what it said,
rather than leaving the part looking untouched — a part that silently reverted
to idle would be picked up again on the next pass and fail the same way.
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.client import CostAccumulator
from inkwell.agent.core import SessionTrace, run_session
from inkwell.agent.pipeline import PipelineListener
from inkwell.agent.session import WritingSessionState
from inkwell.agent.glossary import (
    GlossaryEntry,
    NodeGlossary,
    load_chapter_glossary,
)
from inkwell.manuscript.facts import (
    Consumption,
    Dependency,
    ProposedChange,
    consumption_of,
)
from inkwell.manuscript.links import links_from
from inkwell.manuscript.splice import (
    HeadingLost,
    PartNotFound,
    held_text,
    spliced,
)
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.manuscript.vocabulary import Abbreviation, terms_used

logger = logging.getLogger(__name__)

REVISION_FILE = "part.md"
"""What a part's text is called where a run is handed it.

A file rather than the text inline, because ``revision_target`` takes sources
the extract stage opens, and a part of a book is exactly a document.
"""

PRODUCED_FILE = "produced.md"
"""What a run's prose is called where it is kept, beside the text it revised.

Written before the splice rather than after it, because the pipeline returning
is the moment the prose exists and every step after that can lose it — a run
whose process ends between the two leaves a part reading ``running``, a work
byte-identical to what it was handed, and hours of writing reachable only from
a snapshot nothing points at.

It is also the only record of what a run produced where the splice refused it,
which is what makes a failed run readable rather than merely reported.
"""

type TurnEnding = Literal["rewritten", "parked", "failed"]
"""The three ways one part's turn can end.

Named rather than worked out from an outcome's fields by whoever is asking,
so a fourth way to end costs a literal here instead of a stale condition at
every reader.
"""


class PartRunObservers(BaseModel):
    """What a part run publishes through, where something is watching it.

    The pipeline already takes all four for an ordinary run — this is only the
    set of them, so a part run can be handed the same instruments instead of
    running blind because it was started by a loop rather than by a person.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    listener: PipelineListener | None = Field(
        default=None, description="Where the run's stages and progress are published"
    )
    state: WritingSessionState = Field(
        default_factory=WritingSessionState,
        description="What the run has settled so far — its title, its document",
    )
    cost: CostAccumulator = Field(
        default_factory=CostAccumulator, description="What the run has spent"
    )
    trace: SessionTrace = Field(
        default_factory=SessionTrace, description="Where its turns are recorded"
    )


class PartRunWatch:
    """Something following each part run of a pass while it is still going.

    A pass reports when it has finished, and for a book-length work that is
    long after whoever is watching wanted to know anything: the interesting
    question — what is being worked on right now — is answerable only from
    inside the pass. Told which run holds which part as it opens, asked what
    that run should publish through, and told how it ended.

    Defaults do nothing and hand back instruments nobody reads, so a caller
    that does not care about any of this passes nothing and a part run behaves
    as it always did.
    """

    def opening(self, key: str, session: str) -> PartRunObservers:
        """What the run about to take this part on should publish through."""
        return PartRunObservers()

    def closed(self, key: str, session: str, outcome: PartOutcome) -> None:
        """That run is over, whether it wrote prose, parked, or failed."""


class PartOutcome(BaseModel):
    """What one part's run produced, before any of it is recorded.

    Returned rather than applied, so the loop decides what to do with a run
    that failed, and so the whole of a run's effect on the work is visible in
    one value.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part that ran")
    session: str = Field(default="", description="The run that produced this")
    text: str = Field(default="", description="The prose the run produced")
    consumed: Consumption = Field(
        default_factory=Consumption, description="What the run read of the work"
    )
    changes: tuple[ProposedChange, ...] = Field(
        default=(), description="What the run moved that others may depend on"
    )
    questions: tuple[str, ...] = Field(
        default=(), description="What the run needs answered before it is done"
    )
    failure: str = Field(
        default="", description="Why the run produced nothing, where it did"
    )

    def ended(self) -> TurnEnding:
        """How this turn ended, answered by the outcome rather than about it.

        Named here because only the outcome holds what decides it, and because
        a caller that worked it out from the fields would be a filter that
        goes stale the moment there is a fourth way for a turn to end. The
        order is the order of severity: a run that produced nothing failed
        whatever else it also did, and one that asked something is parked
        rather than finished even though it has prose.
        """
        if self.failure or not self.text:
            return "failed"
        return "parked" if self.questions else "rewritten"

    def succeeded(self) -> bool:
        """Whether there is prose here to put back into the work."""
        return self.ended() != "failed"


def placed(manuscript: Manuscript, node: ManuscriptNode) -> str:
    """Where one part sits, as a sentence a run can be told.

    A part revised without knowing what surrounds it will restate what the
    section before it already established and define what the book defined two
    chapters ago. The tree already knows; a run does not unless it is told.
    """
    trail = [
        held.title
        for held in manuscript.walk()
        if node.key.startswith(held.key) and held.key != node.key
    ]
    within = " > ".join(trail)
    return f"{node.title} — {within} — of {manuscript.title}" if within else node.title


def around(manuscript: Manuscript, node: ManuscriptNode) -> str:
    """The parts a reader reaches just before and just after this one.

    Named with their files, because the point is that they can be read. A run
    told only that it sits "among others" and asked not to repeat them has been
    given a rule about text it cannot see, and the way that comes out is a run
    which goes looking for the work somewhere it can reach — the published
    site, which is a different edition of a book it is holding a page of.
    """
    order = [held for held in manuscript.leaves() if held.path]
    keys = [held.key for held in order]
    if node.key not in keys:
        return ""
    at = keys.index(node.key)
    near = order[max(0, at - 1) : at] + order[at + 1 : at + 2]
    root = Path(manuscript.root)
    return "\n".join(f"- {held.key} {held.title} — {root / held.path}" for held in near)


def part_instruction(
    manuscript: Manuscript, node: ManuscriptNode, reasons: tuple[str, ...] = ()
) -> str:
    """What a part's run is for, said beside the material rather than in a prompt.

    Carried as one more source, which is the mechanism a standing instruction
    already travels by — the material and what it is for reach the planner the
    same way, so nothing here has to reach inside the pipeline to place it.

    Why the part is being revised is included where it is known. "Something you
    depend on changed, and here is what" is the difference between a rewrite
    that addresses the change and one that rewrites the part on general
    principle and moves the book for no reason.
    """
    because = (
        "\n\nWhat has changed since this was last written:\n"
        + "\n".join(f"- {reason}" for reason in reasons)
        if reasons
        else ""
    )
    neighbours = around(manuscript, node)
    reachable = (
        "\n\nThe rest of the work is on disk under "
        f"{manuscript.root}, and you can read any of it. The parts a reader "
        f"reaches either side of this one are:\n{neighbours}\n"
        "Read them before deciding what this part has to establish, and read "
        "further into the work whenever you are about to assert what it says "
        "elsewhere. What the work holds is what is in these files: a published "
        "edition of it is a different version, and checking against that one "
        "instead will have you revising against a book this is not."
        if neighbours
        else ""
    )
    return (
        f"You are revising one part of a larger work: {placed(manuscript, node)}."
        + reachable
        + "\n\nKeep the author's structure, voice, and argument. Do not "
        "reintroduce what the work has already established, and do not rename "
        "anything the shared glossary already settles — look it up and adopt "
        "it. Revise for clarity and for currency, questioning the part's own "
        "claims where they have dated. Return this part alone, opening with "
        "its own heading exactly as it stands." + because
    )


def own_change(key: str) -> ProposedChange:
    """The fact every rewrite publishes: this part's text is not what it was.

    Reaches whatever recorded a structural dependency on this part, and
    nothing else. On a work that cross-links, that is the edge that carries; on
    the Atlas, which links nowhere, it reaches nothing and correctly so — a
    rewrite there propagates through the vocabulary or not at all.
    """
    return ProposedChange(
        dependency=Dependency(kind="node", subject=key),
        detail="its text was rewritten",
    )


def ledger(store: ManuscriptStore, work: str, key: str) -> NodeGlossary:
    """The term ledger one part's run coins into and reads.

    Handed to the run rather than reconciled after it, which is what makes the
    work's vocabulary reach the writers at all: the scope reads the authors'
    declared file first and every sibling part's after it, so a writer asking
    what something is called is answered by the work rather than by an empty
    file, and first-definition-wins is settled while the prose is being written
    instead of discovered once it is too late to change.
    """
    return NodeGlossary(store=store, work=work, key=key)


def named_in(scope: NodeGlossary) -> dict[str, GlossaryEntry]:
    """Every term this part's own file records, by the name it answers to.

    Read once before a run and once after, because the difference is what the
    run settled. Carrying the whole entry rather than the name is what lets a
    term the part redefined count as a change as well as one it newly coined:
    downstream parts lean on what a term *means*, so a name kept over a meaning
    replaced is exactly the change they most need to hear.
    """
    return {
        entry.term.casefold(): entry
        for entry in load_chapter_glossary(scope.own()).terms
    }


def coinages(
    scope: NodeGlossary, before: dict[str, GlossaryEntry], key: str
) -> tuple[ProposedChange, ...]:
    """What a run settled in the ledger, as facts the parts leaning on it hear.

    A name the part already held with the same meaning publishes nothing — the
    write stage reseeds this file each run, so a part that coins again what it
    coined last time has moved nothing, and reporting it would dirty every part
    downstream on every pass and stop the loop ever settling.
    """

    def moved() -> Iterator[ProposedChange]:
        """Each term this run left standing differently from how it found it."""
        for name, entry in named_in(scope).items():
            # lup: ignore[dict-get] — keyed by whatever names the part coined
            held = before.get(name)
            if held is not None and held.meaning == entry.meaning:
                continue
            settled = "coined" if held is None else "redefined"
            yield ProposedChange(
                dependency=Dependency(kind="term", subject=entry.term),
                detail=f"{settled} while revising {key}",
            )

    return tuple(moved())


def written_back(manuscript: Manuscript, node: ManuscriptNode, text: str) -> Path:
    """Put one part's prose back where it came from, disturbing nothing else.

    A part that owns its file has the file replaced; a part that shares one has
    its own span replaced and its siblings left byte-identical. Raises where
    the part's heading is no longer in the file, or where the rewrite arrived
    without one, because the alternative is a rewrite that goes nowhere and a
    run that reports success anyway.
    """
    target = Path(manuscript.root) / node.path
    if not node.heading:
        target.write_text(text, encoding="utf-8")
        return target
    source = target.read_text(encoding="utf-8")
    target.write_text(spliced(source, node.heading, text), encoding="utf-8")
    return target


async def run_part(
    store: ManuscriptStore,
    work: str,
    manuscript: Manuscript,
    node: ManuscriptNode,
    *,
    session_id: str,
    reasons: tuple[str, ...] = (),
    vocabulary: tuple[Abbreviation, ...] = (),
    scratch: Path | None = None,
    observers: PartRunObservers | None = None,
) -> PartOutcome:
    """Take one part through the pipeline and hand back what it produced.

    Keeps the prose in the run's own room before putting it anywhere else, so
    what a run wrote outlives the run whatever becomes of the splice. Writes it
    back into the work, because that is what the next part to read it must see;
    does not touch the work's state, because the loop owns that and needs the
    outcome in hand before deciding anything.
    """
    target = Path(manuscript.root) / node.path
    if not target.is_file():
        return PartOutcome(key=node.key, failure=f"{node.path} is not a file")
    current = held_text(target.read_text(encoding="utf-8"), node)
    if not current:
        return PartOutcome(
            key=node.key,
            failure=f"{node.path} no longer holds a part headed {node.heading!r}",
        )

    room = (
        scratch if scratch is not None else store.work_dir(work) / "runs" / session_id
    )
    room.mkdir(parents=True, exist_ok=True)
    material = room / REVISION_FILE
    material.write_text(current, encoding="utf-8")

    shared = ledger(store, work, node.key)
    shared.own().parent.mkdir(parents=True, exist_ok=True)
    settled = named_in(shared)

    watched = observers if observers is not None else PartRunObservers()
    result = await run_session(
        sources=[str(material), part_instruction(manuscript, node, reasons)],
        material_role="revision_target",
        target_format=manuscript.target_format,
        glossary=shared,
        session_id=session_id,
        listener=watched.listener,
        trace=watched.trace,
        session_state=watched.state,
        cost_accumulator=watched.cost,
    )
    produced = result.output.content if result.output else ""
    if not produced:
        return PartOutcome(
            key=node.key,
            session=session_id,
            failure="the run finished without producing any prose",
        )

    try:
        (room / PRODUCED_FILE).write_text(produced, encoding="utf-8")
        written_back(manuscript, node, produced)
    except (HeadingLost, PartNotFound, OSError) as failure:
        logger.exception("Could not put %s back into %s", node.key, node.path)
        return PartOutcome(key=node.key, session=session_id, failure=str(failure))

    coined = coinages(shared, settled, node.key)
    return PartOutcome(
        key=node.key,
        session=session_id,
        text=produced,
        consumed=consumption_of(
            (
                *terms_used(produced, vocabulary),
                *links_from(manuscript, node, produced),
            )
        ),
        changes=(own_change(node.key), *coined),
        questions=tuple(result.output.open_questions if result.output else ()),
    )
