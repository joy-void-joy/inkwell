"""One part of a work, taken through the writing pipeline and put back.

The build system's compile step. Everything above this decides *which* part to
run and *why*; this runs it, returns its prose to the file it came from, and
records what the run read and what it moved so the parts leaning on it hear.

**A part run writes the part, and inherits from the standing text.** The part's
own text is the run's material, under ``source`` — content to write *from*, and
deliberately not ``revision_target``. Routed as the piece the run replaces, the
standing text becomes the plan: its sections are the run's sections, so the
most pressing thing in a subsection lands fourth because a heading that was
already fourth was already there, and thirty research questions open "The draft
says…". Routed as material, the run answers what this part of the book has to
establish. What the standing text carries that a fresh draft would not is then
:mod:`.inheritance`'s to protect, checked by subtraction rather than by
instruction. Nothing here re-implements a stage; a part is one ordinary run
whose material happens to be a span of somebody's book.

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

**How a part is written comes from the work, not from the part.** A run handed
one subsection and asked to infer its own format is guessing at a book it can
see a page of, and two parts of one work guessing differently is how a textbook
acquires a chapter that reads like a blog post. The work declares it once at
import and every part inherits it — and the same argument reaches past the
format to how a part is drafted and which stages its runs perform. A subsection
is already the unit the parallel-writer split exists to make manageable, so
splitting it again buys nine writers and a merge to draft what one writer
drafts in one pass; the work says so once rather than the setting saying it for
every run of every project.

**The lease is the standing.** A part is marked ``running`` with the session
holding it before anything starts, and a part already held is one the loop
will not schedule twice. A run that fails leaves ``failed`` with what it said,
rather than leaving the part looking untouched — a part that silently reverted
to idle would be picked up again on the next pass and fail the same way.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lup.channels.models import utc_now

from inkwell.agent.client import (
    AgentSurface,
    AgentUpdate,
    CostAccumulator,
    quota_wait_message,
)
from lup.runtime.quota import QuotaWaitEvent
from inkwell.agent.core import SessionTrace, run_session
from inkwell.agent.pipeline import PipelineListener
from inkwell.agent.session import WritingSessionState
from lup.runtime.models import AnyTurnBlock
from lup.telemetry.blocks import extract_block_info
from inkwell.agent.glossary import (
    GlossaryEntry,
    NodeGlossary,
    load_chapter_glossary,
)
from inkwell.agent.models import ArticlePlan
from inkwell.manuscript.brief import (
    BRIEF_FILE,
    BriefWriter,
    CorpusPusher,
    EditorialEvidence,
    LocalCorpusPusher,
    compose,
    evidence_for,
    subsection_topic,
)
from inkwell.manuscript.planner import article_plan
from inkwell.manuscript.budget import LengthBudget, budget_for
from inkwell.manuscript.facts import (
    Consumption,
    Dependency,
    ProposedChange,
    bears_on,
    consumption_of,
)
from inkwell.manuscript.findings import WorkFindings, briefing
from inkwell.manuscript.inheritance import (
    Adoption,
    Dropped,
    InheritanceReader,
    settle,
)
from inkwell.manuscript.inventory import Figure, inventory_of
from inkwell.manuscript.links import links_from
from inkwell.manuscript.splice import (
    HeadingLost,
    PartNotFound,
    held_text,
    spliced,
)
from inkwell.manuscript.state import digest_of
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.manuscript.vocabulary import Abbreviation, terms_used

logger = logging.getLogger(__name__)

REVISION_FILE = "part.md"
"""What a part's text is called where a run is handed it.

A file rather than the text inline, because the extract stage opens sources and
a part of a book is exactly a document. It is also what the inheritance pass
reads the successor against, which is the second reason it is a file: two
passes need the same standing text, and the second runs after the first has
finished with everything it held.
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

HANDOFF_FILE = "handoff.json"
"""Proof that adopted prose reached both its source file and live surface."""

RUN_FILE = "run.json"
"""What a run says about itself, written into its room before anything else.

The record that makes a history symmetric. A part's build stamp names the run
that built it, so a run that produced prose is findable from the part — and a
run that produced nothing is findable from nothing at all, which makes the runs
most worth looking at the ones with the weakest linkage. A room whose first
write says which work, which part, and when closes that: every run is
discoverable from its own room, and how far it got is which of the files beside
this one exist.
"""

type TurnEnding = Literal["rewritten", "parked", "failed"]
"""The three ways one part's turn can end.

Named rather than worked out from an outcome's fields by whoever is asking,
so a fourth way to end costs a literal here instead of a stale condition at
every reader.
"""


class PartRun(BaseModel):
    """What one run was about, written where the run's own files are.

    Everything else in a run's room is an output — the material it was handed,
    the prose it made, the successor that was settled, the audit of what was
    dropped. This is the only thing that says whose run it was, and it is
    written first so that a run which produced none of the rest still says so.
    """

    model_config = ConfigDict(frozen=True)

    session: str = Field(description="The run")
    work: str = Field(description="The work it was a run of")
    key: str = Field(description="The part it was about")
    title: str = Field(default="", description="How the work names that part")
    opened_at: datetime = Field(default_factory=utc_now, description="When it started")
    reasons: tuple[str, ...] = Field(
        default=(), description="Why the part was picked up"
    )


class PartResumption(BaseModel):
    """Where a part's run picks the pipeline up, rather than starting one.

    A part run is one ordinary run, so it keeps the same per-stage snapshots
    any run does and has the same right to be continued from one. What it
    lacked was anybody able to say so: the loop only ever started runs, so a
    part that lost its reviewers to an expired token had to be cleared back to
    idle and written from nothing — paying for the plan, the research and the
    draft a second time to reach the stage that actually failed.

    Which run is resumed is the session the part is already run under, so it is
    not repeated here. ``redo`` names a stage to rewind to and run again;
    empty picks up from the last stage that finished, which is what a failure
    *inside* a stage wants — that stage never checkpointed, so continuing from
    the one before it is already redoing it.
    """

    model_config = ConfigDict(frozen=True)

    redo: str = Field(
        default="",
        description="Stage to rewind to and run again, discarding it and "
        "everything after; empty picks up from the last stage that finished",
    )


class PartHandoff(BaseModel):
    """The settled successor that was committed after the pipeline draft."""

    model_config = ConfigDict(frozen=True)

    session: str = Field(description="The run that produced the successor")
    key: str = Field(description="The part whose source was replaced")
    adopted_digest: str = Field(description="Fingerprint of the adopted prose")
    working_doc: str = Field(
        default="", description="Live document updated, empty where none exists"
    )
    completed_at: datetime = Field(
        default_factory=utc_now, description="When every declared surface held it"
    )


class PartPublisher(ABC):
    """Where adopted prose replaces the pipeline draft on its live surface."""

    @abstractmethod
    async def publish(self, doc_id: str, text: str) -> None:
        """Replace the live final surface with settled prose."""


class GoogleDocPublisher(PartPublisher):
    """The working Google Doc as the author follows it."""

    async def publish(self, doc_id: str, text: str) -> None:
        from inkwell.agent.tools.google_docs import write_with_continuation

        await write_with_continuation(doc_id, "Final", text)


def opened(
    room: Path, work: str, node: ManuscriptNode, session: str, reasons: tuple[str, ...]
) -> Path:
    """Say whose run this is, before it does anything that could fail."""
    path = room / RUN_FILE
    path.write_text(
        PartRun(
            session=session,
            work=work,
            key=node.key,
            title=node.title,
            reasons=reasons,
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    return path


def runs_under(root: Path) -> tuple[PartRun, ...]:
    """Every run this work has recorded, newest first.

    Read off the rooms rather than off the build stamps, which is what makes it
    symmetric: a stamp exists only where a run produced prose, so a history
    read from stamps is a history of successes with the failures missing.
    """

    def held() -> Iterator[PartRun]:
        """Each room that says whose run it was."""
        for path in sorted(root.glob(f"*/{RUN_FILE}")):
            try:
                yield PartRun.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, OSError):
                logger.warning("Unreadable run record at %s", path, exc_info=True)

    return tuple(sorted(held(), key=lambda one: one.opened_at, reverse=True))


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

    def surface(self) -> AgentSurface:
        """Build the one observation owner for the part's complete task tree."""
        listener = self.listener

        async def forward_block(block: AnyTurnBlock, prefix: str) -> None:
            if listener is not None:
                info = extract_block_info(block.telemetry_block)
                await listener.on_block(info.label, info.content, prefix)

        async def forward_agent(update: AgentUpdate) -> None:
            if listener is not None:
                await listener.on_agent(update)

        async def forward_quota(event: QuotaWaitEvent) -> None:
            if listener is not None:
                await listener.on_progress(quota_wait_message(event))

        return AgentSurface(
            block_callback=forward_block if listener is not None else None,
            agent_callback=forward_agent if listener is not None else None,
            quota_callback=forward_quota if listener is not None else None,
            trace_logger=self.trace.trace_logger,
            cost_accumulator=self.cost,
        )


class PartRunAgents(BaseModel):
    """Every reader a part run buys from a model, in one place.

    One object rather than a parameter apiece, for the reason
    :class:`PartRunObservers` is one: the set grows. Named separately, a caller
    that stubs two of three seams silently gets the real thing for the third —
    which for a test means a unit test that reaches a model, and finds out by
    taking four minutes instead of one second. Handed as a set, "did I stub
    everything" is one question with one answer.

    Defaults are nothing, so a run that passes none buys the real readers and
    behaves as it always did.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    briefing: BriefWriter | None = Field(
        default=None, description="Who plans the part before the pipeline writes it"
    )
    corpus: CorpusPusher | None = Field(
        default=None, description="Who pushes subsection-scoped corpus evidence"
    )
    inheriting: InheritanceReader | None = Field(
        default=None,
        description="Who settles the fresh draft against the standing text",
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
    dropped: tuple[Dropped, ...] = Field(
        default=(),
        description="What the standing text carried that the successor does "
        "not, each with the reason it does without it",
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


async def unaccounted_said(
    adoption: Adoption,
    node: ManuscriptNode,
    working_doc: str,
    listener: PipelineListener | None,
) -> None:
    """Say what the successor stopped carrying and nobody explained.

    The part is adopted either way, so this is the whole of the author's
    warning and it goes everywhere the author might be: the run's own surface
    while it is watching, and the part's document, where it waits for whenever
    they open it. A document that cannot take the comment is not worth failing
    an adopted part over — the loss is on the outcome and in the run's record
    regardless.
    """
    said = (
        f"Adopted {node.title!r} with {len(adoption.unexplained())} thing(s) the "
        f"standing text carried and this rewrite does not, none of them "
        f"accounted for:\n{adoption.unaccounted.render()}"
    )
    logger.warning("Adopted %s with unaccounted losses: %s", node.key, said)
    if listener is not None:
        await listener.on_progress(said)
    if not working_doc:
        return
    from inkwell.agent.tools.google_docs import do_insert_comment

    try:
        await do_insert_comment(working_doc, said)
    except (RuntimeError, OSError) as failure:
        logger.warning("Could not comment the unaccounted losses: %s", failure)


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


def licensed(reasons: tuple[str, ...]) -> str:
    """What this run is licensed to change, which is what asked for it.

    Why the part is being revised is what bounds the revision. "Something you
    depend on changed, and here is what" is the difference between a rewrite
    that addresses the change and one that rewrites the part on general
    principle and moves the book for no reason — and, pointed the other way,
    a reason about what the part is *for* is the only thing that licenses
    reshaping it. Nothing else in the run is in a position to decide what this
    subsection should open with.

    A part with no reasons is one somebody asked for or one nothing has built,
    and saying so is better than saying nothing: a writer told only to write
    infers a licence from the size of the subject.
    """
    if not reasons:
        return (
            "\n\nNothing specific is outstanding on this part — it was asked "
            "for, or the work has never built it. Write it as it should stand, "
            "and treat its scale and its place in the chapter as settled."
        )
    asked = "\n".join(f"- {reason}" for reason in reasons)
    return (
        f"\n\nWhat has changed since this was last written:\n{asked}\n"
        "That list is the scope. A reason naming a claim asks you to settle "
        "that claim; a reason about what this part is for licenses reshaping "
        "it. Where nothing here asks for a change, the standing text's "
        "argument is the best evidence there is of what this part establishes."
    )


def constrained(figures: tuple[Figure, ...]) -> str:
    """The figures this part carries, handed over as fixed rather than inferred.

    A number and an image path are facts about the book, not choices a writer
    makes: Figure 2.13 is the thirteenth figure of chapter two because twelve
    come before it in chapters this run cannot see. A writer left to infer them
    renumbers from one, or composes a figure of its own around a path it found
    in the material, and either way the chapter acquires two conventions.
    """
    if not figures:
        return ""
    listed = "\n".join(f"- {figure.render()} — {figure.image}" for figure in figures)
    return (
        f"\n\nThis part carries {len(figures)} figure(s), and their numbering is "
        f"the book's rather than yours:\n{listed}\n"
        "Reproduce each block exactly as the material spells it — same number, "
        "same image path, same caption — wherever your argument wants it. Never "
        "renumber one and never build a new figure around an image path."
    )


def part_instruction(
    manuscript: Manuscript,
    node: ManuscriptNode,
    reasons: tuple[str, ...] = (),
    *,
    figures: tuple[Figure, ...] = (),
    budget: LengthBudget | None = None,
    research: str = "",
) -> str:
    """What a part's run is for, said beside the material rather than in a prompt.

    Carried as one more source, which is the mechanism a standing instruction
    already travels by — the material and what it is for reach the planner the
    same way, so nothing here has to reach inside the pipeline to place it.

    What it does *not* say is that the part's structure is to be kept. That
    instruction, sitting beside material routed as the piece being replaced, is
    what produced a subsection whose seven headings survived a full rewrite in
    their original order with thirty-four new ones hung off them. The standing
    text reaches the run as material; its sequence is where the last draft put
    things, and this says so.
    """
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
    scale = f"\n\n{budget.render()}" if budget is not None and budget.render() else ""
    return (
        f"You are writing one part of a larger work: {placed(manuscript, node)}."
        + reachable
        + "\n\nThe material you are given is this part as the work holds it "
        "now. It is the current state of the passage and the best evidence of "
        "what the passage is for — it is not the shape of what you are "
        "writing. The order its headings happen to be in is where the last "
        "draft left them, and a development that matters more than everything "
        "around it does not belong fourth because a heading was already there."
        + constrained(figures)
        + scale
        + "\n\nKeep the author's voice. Do not reintroduce what the work has "
        "already established, and do not rename anything the shared glossary "
        "already settles — look it up and adopt it. Question the part's own "
        "claims where they have dated. Return this part alone, opening with "
        "its own heading exactly as the material spells it."
        + licensed(reasons)
        + (f"\n\n{research}" if research else "")
    )


async def planned_for(
    store: ManuscriptStore,
    work: str,
    manuscript: Manuscript,
    node: ManuscriptNode,
    material: Path,
    room: Path,
    *,
    evidence: EditorialEvidence,
    writer: BriefWriter | None,
) -> ArticlePlan | None:
    """The plan this run works to, from whichever reader has one.

    A book planner's where a pass ran one, because that reader saw what this
    part is about to do to its siblings and this part cannot. This part's own
    deriver otherwise, because a run testing one subsection must not trigger a
    book read to get a plan. Both emit the same model over the same inputs, so
    nothing after this can tell which reader composed it — which is the whole
    reason there are two.
    """
    held = store.load_briefs(work).brief_for(
        node.key,
        digest_of(material.read_text(encoding="utf-8")),
        evidence.digest(),
    )
    if held is not None:
        logger.info("Running %s to the brief a book planner composed", node.key)
        plan = article_plan(manuscript, node, held)
        (room / BRIEF_FILE).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        return plan
    return await compose(
        manuscript,
        node,
        evidence,
        room,
        writer=writer,
    )


def held_brief(room: Path) -> ArticlePlan | None:
    """The brief this run was already working to, off its own room.

    Written by whichever reader composed it, so a resume reads one file rather
    than working out which of the two to ask again.
    """
    path = room / BRIEF_FILE
    if not path.is_file():
        return None
    try:
        return ArticlePlan.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        logger.exception("The brief saved under %s could not be read back", room)
        return None


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
    agents: PartRunAgents | None = None,
    found: WorkFindings | None = None,
    publisher: PartPublisher | None = None,
    resuming: PartResumption | None = None,
) -> PartOutcome:
    """Run one part with observation active from its first phase to its last."""
    watched = observers if observers is not None else PartRunObservers()
    watched.trace.open(session_id)
    with watched.surface().activate():
        try:
            return await execute_part(
                store,
                work,
                manuscript,
                node,
                session_id=session_id,
                reasons=reasons,
                vocabulary=vocabulary,
                scratch=scratch,
                observers=watched,
                agents=agents,
                found=found,
                publisher=publisher,
                resuming=resuming,
            )
        finally:
            watched.trace.save()


async def execute_part(
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
    agents: PartRunAgents | None = None,
    found: WorkFindings | None = None,
    publisher: PartPublisher | None = None,
    resuming: PartResumption | None = None,
) -> PartOutcome:
    """Take one part through the pipeline and hand back what it produced.

    Four things in order, and the order is the point. The brief is composed
    from what only the work knows — where the part sits, what surrounds it,
    what it is entitled to, what the research holds on it — because nothing
    inside a run holding one subsection is in a position to plan that
    subsection. The pipeline then writes the part from the material, working
    to that plan; the inheritance pass settles the draft against the text the
    work holds, so nothing the author put there goes missing without a reason;
    and only then is the settled text spliced in. Each step's output is kept in
    the run's own room before the next one runs, so what a run wrote outlives
    the run whatever becomes of the steps after it.

    Does not touch the work's state, because the loop owns that and needs the
    outcome in hand before deciding anything.

    ``found`` is what the research has been placed on this part, read off the
    store where a caller does not already hold it. A pass holds it, because a
    pass reads it once for two hundred parts.

    ``resuming`` continues the run this session already holds instead of
    opening one. The brief is then read back from the run's own room rather
    than composed again — a resumed run has to work to the plan its snapshots
    were written against, and re-deriving would spend the prose-blind planning
    call to arrive at a plan the stages downstream have already been built on.
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
    opened(room, work, node, session_id, reasons)
    material = room / REVISION_FILE
    material.write_text(current, encoding="utf-8")

    shared = ledger(store, work, node.key)
    shared.own().parent.mkdir(parents=True, exist_ok=True)
    settled = named_in(shared)

    watched = observers if observers is not None else PartRunObservers()
    reading = agents if agents is not None else PartRunAgents()
    work_findings = found if found is not None else store.load_findings(work)
    research = briefing(work_findings, node.key)
    figures = inventory_of(current).figures
    budget = budget_for(manuscript, node)
    if watched.state is not None:
        watched.state.title = node.title
    if watched.listener is not None:
        await watched.listener.on_stage("brief", "Planning this part in its work")
        await watched.listener.on_progress("Retrieving corpus evidence for this part")
    corpus = await (reading.corpus or LocalCorpusPusher()).push(
        subsection_topic(manuscript, node), current
    )
    evidence = evidence_for(
        manuscript,
        node,
        work_findings,
        reasons=reasons,
        corpus=corpus,
        figures=figures,
        budget=budget,
    )
    instruction = part_instruction(
        manuscript,
        node,
        reasons,
        figures=figures,
        budget=budget,
        research=research,
    )
    if watched.listener is not None:
        await watched.listener.on_progress(
            "Reading back this part's editorial brief"
            if resuming is not None
            else "Composing this part's editorial brief"
        )
    plan = (
        held_brief(room)
        if resuming is not None
        else await planned_for(
            store,
            work,
            manuscript,
            node,
            material,
            room,
            evidence=evidence,
            writer=reading.briefing,
        )
    )
    if plan is None:
        return PartOutcome(
            key=node.key,
            session=session_id,
            failure=(
                f"no brief saved under {room}; nothing to resume against"
                if resuming is not None
                else "prose-blind planning produced no brief; pipeline not started"
            ),
        )
    result = await run_session(
        sources=[str(material), instruction],
        material_role="source",
        material_authoritative=False,
        review_profile="manuscript_part",
        asking="handback",
        target_format=manuscript.target_format,
        writer_mode=manuscript.writer_mode,
        skipped_stages=list(manuscript.skipped_stages),
        plan=plan,
        glossary=shared,
        existing_doc_id=store.load_docs(work).working_doc(node.key) or None,
        session_id=session_id,
        resume_session_id=session_id if resuming is not None else None,
        restart_from_stage=resuming.redo if resuming and resuming.redo else None,
        listener=watched.listener,
        trace=watched.trace,
        session_state=watched.state,
        cost_accumulator=watched.cost,
    )
    # Recorded before the prose is judged, because the document exists either
    # way: a run that produced nothing still made one, and forgetting it is how
    # a part that has failed twice comes to own three documents.
    if result.output and result.output.google_doc_id:
        store.publish_docs(
            work,
            store.load_docs(work).with_working_doc(
                node.key, result.output.google_doc_id
            ),
        )
    working_doc = result.output.google_doc_id if result.output else ""

    produced = result.output.content if result.output else ""
    if not produced:
        return PartOutcome(
            key=node.key,
            session=session_id,
            failure="the run finished without producing any prose",
        )
    (room / PRODUCED_FILE).write_text(produced, encoding="utf-8")

    if watched.listener is not None:
        await watched.listener.on_progress(
            "Reconciling the fresh draft with the standing text"
        )
    adoption = await settle(
        material, room / PRODUCED_FILE, room, reader=reading.inheriting
    )
    if not adoption.adoptable():
        logger.warning("Nothing was adopted for %s: %s", node.key, adoption.render())
        return PartOutcome(key=node.key, session=session_id, failure=adoption.render())
    if not adoption.settled():
        await unaccounted_said(adoption, node, working_doc, watched.listener)

    try:
        written_back(manuscript, node, adoption.text)
    except (HeadingLost, PartNotFound, OSError) as failure:
        logger.exception("Could not put %s back into %s", node.key, node.path)
        return PartOutcome(key=node.key, session=session_id, failure=str(failure))

    if working_doc:
        try:
            await (publisher or GoogleDocPublisher()).publish(
                working_doc, adoption.text
            )
        except Exception as failure:
            logger.exception("Could not publish adopted prose for %s", node.key)
            return PartOutcome(key=node.key, session=session_id, failure=str(failure))

    (room / HANDOFF_FILE).write_text(
        PartHandoff(
            session=session_id,
            key=node.key,
            adopted_digest=digest_of(adoption.text),
            working_doc=working_doc,
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    coined = coinages(shared, settled, node.key)
    return PartOutcome(
        key=node.key,
        session=session_id,
        text=adoption.text,
        dropped=(*adoption.dropped, *adoption.unexplained()),
        consumed=consumption_of(
            (
                bears_on(node.key),
                *terms_used(adoption.text, vocabulary),
                *links_from(manuscript, node, adoption.text),
            )
        ),
        changes=(own_change(node.key), *coined),
        questions=tuple(result.output.open_questions if result.output else ()),
    )
