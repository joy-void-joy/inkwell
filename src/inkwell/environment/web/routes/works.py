"""REST endpoints for a work of many parts and the loop over it.

A work is not a session and gets a resource of its own. A session is one run
with a beginning and an end; a work outlives every run against it, is revised
over months, and is the thing an author actually watches. Filing it under
``sessions`` would make the tree a view of whichever run happened to be open.

What this surface is *for* is the part a command line answers badly. Status
reads fine as text. A tree of two hundred parts, each with a state and a
reason, which an author scans to find the four that are outstanding and asks
"what would change if I touched this" — that is a tree, and a terminal renders
it as two hundred lines somebody scrolls past.

**And it is where a parked question gets answered.** A part that asked
something waits, costing nothing, until somebody replies. Every other route
here reports; this one is the reason the loop can run unattended at all.
"""

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.agent.client import AgentUpdate
from inkwell.agent.cohort import roster_of
from inkwell.agent.config import manuscript_store
from inkwell.agent.glossary import DECLARED_VOCABULARY, load_chapter_glossary
from inkwell.agent.stages import unknown_format
from inkwell.environment.web.models import AgentEvent, SessionStatus
from inkwell.environment.web.session_manager import SessionHandle, SessionManager
from inkwell.environment.web.work_loops import (
    LoopAlreadyRunning,
    WorkLoopManager,
    WorkLoopStatus,
)
from inkwell.manuscript.facts import FACT_KINDS, Dependency
from inkwell.manuscript.graph import consumers, missing_root, readings, sweep
from inkwell.manuscript.loop import (
    DEFAULT_CONCURRENCY,
    DEFAULT_PASSES,
    NotPicked,
    WorkAlreadyRunning,
    WorkLease,
    narrowed,
    schedulable,
    unpicked,
    work_is_running,
)
from inkwell.manuscript.inheritance import ADOPTED_FILE
from inkwell.manuscript.mailbox import PartAnswer, PartMailbox, PartQuestion
from inkwell.manuscript.recording import WorkImport, import_work
from inkwell.manuscript.runner import PRODUCED_FILE, runs_under
from inkwell.manuscript.state import (
    NodeStanding,
    NodeVerdict,
    Staleness,
    WorkState,
)
from inkwell.manuscript.store import ManuscriptStore, WorkId
from inkwell.manuscript.tree import (
    DEFAULT_WORK_FORMAT,
    Manuscript,
    ManuscriptNode,
)
from inkwell.manuscript.vocabulary import Abbreviation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/works", tags=["works"])


class LoopHolder(BaseModel):
    """Where the process keeps its work loops, so a route reaches them.

    Filled in at app startup, the same way the session manager is, rather than
    read off a module global — a route that built its own manager would lose
    every loop the moment a second worker answered.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    loops: WorkLoopManager | None = None

    def require(self) -> WorkLoopManager:
        """The manager, for the routes that cannot answer without one."""
        if self.loops is None:
            raise HTTPException(
                status_code=503, detail="The work loop manager is not running"
            )
        return self.loops

    def status_of(self, work: str) -> WorkLoopStatus | None:
        """Where a loop over this work stands, or nothing where none could be.

        Answered rather than required, because reading a work is not asking to
        run one: a tree that refused to render because no loop had ever started
        would make the loop a precondition for looking at the book.
        """
        return self.loops.status(work) if self.loops is not None else None

    def running(self, work: str) -> bool:
        """Whether a loop over this work is in flight in this process."""
        return self.loops is not None and self.loops.running(work)

    def session_manager(self) -> SessionManager | None:
        """The session surface receiving this loop's part-run events."""
        return self.loops.sessions if self.loops is not None else None


holder = LoopHolder()
"""This process's loops, set at startup by :func:`set_loops`."""


def set_loops(manager: WorkLoopManager) -> None:
    """Hand the routes the manager the app owns."""
    holder.loops = manager


def get_loops() -> WorkLoopManager:
    """The manager these routes run loops through."""
    return holder.require()


def loop_running(store: ManuscriptStore, work: str) -> bool:
    """Whether this process or another one owns the work's loop."""
    return holder.running(work) or work_is_running(store, work)


def loop_status(store: ManuscriptStore, work: str) -> WorkLoopStatus | None:
    """The local report, with cross-process ownership reflected as running."""
    local = holder.status_of(work)
    if local is not None and local.running:
        return local
    if work_is_running(store, work):
        return WorkLoopStatus(work=work, running=True)
    return local


def claim_mutation(store: ManuscriptStore, work: str) -> WorkLease:
    """Reserve a work for one state mutation, refusing an active loop."""
    try:
        return WorkLease.acquire(store, work)
    except WorkAlreadyRunning as already:
        raise HTTPException(status_code=409, detail=str(already)) from already


class WorkSummary(BaseModel):
    """One work as a listing shows it."""

    id: str = Field(description="The slug the work is recorded under")
    title: str = Field(description="What the work is called")
    parts: int = Field(description="How many parts a run can be about")
    outstanding: int = Field(description="How many of them have work left")


type FlightStanding = SessionStatus | Literal["queued", "orphaned"]
"""What is happening to a leased part, beyond what its run would say.

Two standings no session has: one whose turn has not come, and one whose run
is gone. Both look identical in the state — the part is leased — and they want
opposite things done about them.
"""


class PartInFlight(BaseModel):
    """One part being worked on right now, as something surveying it reads.

    The question a work page could not answer: a pass over a long book reports
    when it is over, so an author who started one had a spinner and a settled
    tree and no way to tell which subsection was being written, by which run,
    or whether anything was happening at all. Joined from the two halves that
    each hold some of it — the work's state knows which part is leased and by
    whom, the session knows how far in it is and what it has spent.
    """

    work: str = Field(description="The work it belongs to")
    key: str = Field(description="The part being written")
    title: str = Field(description="How the work names that part")
    session: str = Field(description="The run holding it")
    since: datetime = Field(description="When it was picked up")
    stage: str = Field(default="", description="Where in the pipeline it has got to")
    status: FlightStanding = Field(
        default="running", description="What is actually happening to the part"
    )
    cost_usd: float = Field(default=0.0, description="What it has spent so far")
    doc_url: str = Field(default="", description="The document it is writing into")
    reason: str = Field(default="", description="Why it was picked up")


class PartNode(BaseModel):
    """One part of a work as the tree renders it.

    Flat, with a parent rather than nested children, because the tree is
    re-rendered on every state change and a flat list diffs cheaply. The
    client rebuilds the nesting it wants from ``parent`` and ``depth``.
    """

    key: str = Field(description="The part's identity")
    title: str = Field(description="How the work names it")
    kind: str = Field(description="What the part is")
    parent: str = Field(default="", description="Key of the part above it")
    depth: int = Field(default=0, description="How far down the tree it sits")
    path: str = Field(default="", description="File holding its text, where one does")
    leaf: bool = Field(default=False, description="Whether a run can be about it")
    staleness: Staleness = Field(
        default="fresh", description="Whether it is out of date, and why"
    )
    standing: NodeStanding = Field(
        default="idle", description="What this system was told about it"
    )
    reasons: tuple[str, ...] = Field(default=(), description="What made it outstanding")
    questions: int = Field(default=0, description="How many answers it is waiting on")
    session: str = Field(
        default="",
        description="The run holding this part, or the one that last failed on it — "
        "what a reader follows to see what actually happened",
    )
    below: int = Field(
        default=0, description="Leaf parts beneath it, itself where it is one"
    )
    outstanding_below: int = Field(
        default=0, description="How many of those have work left"
    )


class WorkTree(BaseModel):
    """A whole work, its state, and what it is waiting on."""

    id: str = Field(description="The work")
    title: str = Field(description="What it is called")
    root: str = Field(default="", description="Where its chapters were read from")
    target_format: str = Field(
        default=DEFAULT_WORK_FORMAT,
        description="What every run against this work writes its part as",
    )
    rooted: bool = Field(
        default=True,
        description="Whether that directory is still there. False makes every "
        "part's lost text one fact about the checkout rather than two hundred "
        "about the prose",
    )
    nodes: tuple[PartNode, ...] = Field(
        default=(), description="Every part, in reading order"
    )
    outstanding: int = Field(default=0, description="Parts with work left")
    blocked: int = Field(
        default=0, description="Parts outstanding that no run can put right"
    )
    settled: bool = Field(default=True, description="Whether the work is at rest")
    loop: WorkLoopStatus | None = Field(
        default=None, description="Where a loop over this work stands, if one has run"
    )
    in_flight: tuple[PartInFlight, ...] = Field(
        default=(),
        description="What is being written right now. Carried on the tree "
        "rather than fetched beside it, so the page that shows the work "
        "cannot show it as quiet while a run is halfway through a part",
    )


class QuestionView(BaseModel):
    """One open question as the tree shows it."""

    id: str = Field(description="What the question is addressed by")
    asker: str = Field(description="Key of the part waiting on it")
    addressed_to: str = Field(default="", description="Key of the part it went to")
    prompt: str = Field(description="What was asked")


class AnswerRequest(BaseModel):
    """An answer somebody is giving to a parked question."""

    value: str = Field(description="The answer, in the answerer's own words")
    answered_by: str = Field(default="", description="Who is answering")


class RequestRevision(BaseModel):
    """An author asking for one part to be revised."""

    reason: str = Field(default="", description="What they want done to it")


class ReachedBy(BaseModel):
    """What changing one thing would put out of date."""

    kind: str = Field(description="Which way parts depend on it")
    subject: str = Field(description="The term, claim, or part")
    parts: tuple[str, ...] = Field(default=(), description="What leans on it")


class ImportRequest(BaseModel):
    """A work somebody is asking this installation to record."""

    work: WorkId = Field(description="Slug to record it under")
    chapters: str = Field(description="Directory holding the work's chapters")
    title: str = Field(default="", description="What to call it")
    target_format: str = Field(
        default=DEFAULT_WORK_FORMAT,
        description="What every part of it is written as; every run inherits it",
    )
    vocabulary: str = Field(
        default="", description="The declared abbreviations file, where it is elsewhere"
    )
    adopt: bool = Field(
        default=True,
        description="Stamp every part as built from the text it already holds. Off "
        "treats the whole work as unwritten, which is a rewrite of everything",
    )


class RunRequest(BaseModel):
    """What a loop over a work should be allowed to do.

    Every default is the one the command line takes, so the same work run from
    either surface costs the same and stops in the same places.
    """

    passes: int = Field(
        default=DEFAULT_PASSES, ge=1, description="Passes before stopping and reporting"
    )
    limit: int = Field(
        default=0, ge=0, description="Most parts to run per pass; 0 for every one"
    )
    parts: tuple[str, ...] = Field(
        default=(),
        description="Run only these parts, by key. Empty runs whatever is "
        "outstanding. Narrows the sweep rather than overriding it, so a part "
        "that is up to date stays up to date",
    )
    concurrency: int = Field(
        default=DEFAULT_CONCURRENCY, ge=1, description="How many parts run at once"
    )
    reconcile: bool = Field(
        default=True, description="Read each wave's rewrites against each other after"
    )


class RunPartRequest(BaseModel):
    """The optional instruction attached to running one selected part."""

    reason: str = Field(default="", description="What should change in this part")


class WouldRun(BaseModel):
    """What a loop would pick up, answered without spending anything.

    The reply to the question worth asking first: every part is a whole pipeline
    run, so a browser that offered only a start button would be offering to
    spend an unknown amount of money on one click.
    """

    work: str = Field(description="The work")
    parts: tuple[NodeVerdict, ...] = Field(
        default=(), description="What this pass would pick up, in order"
    )
    outstanding: int = Field(
        default=0, description="Parts outstanding in total, past this pass's limit"
    )
    blocked: tuple[NodeVerdict, ...] = Field(
        default=(), description="Parts outstanding that no run could put right"
    )
    passed_over: tuple[NotPicked, ...] = Field(
        default=(),
        description="Parts that were named but will not run, and why not — so "
        "asking for one subsection and getting an empty pass says which it was",
    )


def tree_of(store: ManuscriptStore, work: str) -> Manuscript:
    """One recorded work, or a 404 naming what to do about it."""
    held = store.load_tree(work)
    if held is None:
        raise HTTPException(
            status_code=404,
            detail=f"No work called {work!r} is recorded — import it first",
        )
    return held


def vocabulary_of(store: ManuscriptStore, work: str) -> tuple[Abbreviation, ...]:
    """The terms a work's authors declared, as the sweep matches them."""
    held = load_chapter_glossary(store.glossary_path(work, DECLARED_VOCABULARY))
    return tuple(
        Abbreviation(term=entry.term, meaning=entry.meaning) for entry in held.terms
    )


def beneath(tree: Manuscript, key: str) -> tuple[str, ...]:
    """Every leaf part at or under one key.

    What makes a chapter row worth reading. A container has no verdict of its
    own, so without this the tree offers two hundred leaf rows and no way to see
    that all four outstanding parts are in chapter 6 — which is the question an
    author opens the tree with.
    """
    node = tree.node(key)
    return tuple(found.key for found in node.leaves()) if node else ()


def parent_of(tree: Manuscript, key: str) -> str:
    """The nearest ancestor of a key among the work's own parts."""
    above = [
        node.key
        for node in tree.walk()
        if key.startswith(f"{node.key}/") and node.key != key
    ]
    return max(above, key=len) if above else ""


def part_node(
    tree: Manuscript,
    state: WorkState,
    judged: dict[str, NodeVerdict],
    waiting: tuple[PartQuestion, ...],
    node: ManuscriptNode,
) -> PartNode:
    """One part as every route renders it.

    One builder because there is more than one route that answers with a part —
    the tree, and each of the two that change one — and a field added for the
    tree but missed at the others would have the same part read differently
    depending on which call the browser last made.

    A part above the leaves has no verdict of its own, nothing runs against it,
    and reads as fresh: a chapter is never out of date, the parts under it are.
    """
    verdict = judged[node.key] if node.key in judged else None
    leaves = beneath(tree, node.key)
    record = state.record(node.key)
    stamp = state.stamp(node.key)
    return PartNode(
        key=node.key,
        title=node.title,
        kind=node.kind,
        parent=parent_of(tree, node.key),
        depth=node.key.count("/"),
        path=node.path,
        leaf=not node.children,
        staleness=verdict.staleness if verdict else "fresh",
        standing=state.standing(node.key),
        reasons=verdict.reasons if verdict else (),
        questions=len([held for held in waiting if held.asker == node.key]),
        # The holder while a run has it or failed on it, and the stamp's run
        # once one succeeded — so the link survives the rewrite that cleared
        # the lease.
        session=(record.holder if record else "") or (stamp.run if stamp else ""),
        below=len(leaves),
        outstanding_below=len(
            [key for key in leaves if key in judged and judged[key].dirty()]
        ),
    )


def one_part(store: ManuscriptStore, work: str, key: str) -> PartNode:
    """One part of a work, read back as the tree would show it.

    What the routes that change a part answer with, so what the browser puts in
    place of a row is the same shape the next poll will bring.
    """
    tree = tree_of(store, work)
    node = tree.node(key)
    if node is None:
        raise HTTPException(status_code=404, detail=f"{work} has no part {key!r}")
    state = store.load_state(work)
    found = sweep(state, readings(tree, vocabulary_of(store, work)))
    return part_node(
        tree,
        state,
        {verdict.key: verdict for verdict in found.verdicts},
        PartMailbox(root=store.work_dir(work)).open(),
        node,
    )


@router.get("")
def list_works() -> list[WorkSummary]:
    """Every work this installation has recorded."""
    store = manuscript_store()

    def summarised() -> Iterator[WorkSummary]:
        """Each recorded work with its title and what it has outstanding."""
        for name in store.works():
            tree = store.load_tree(name)
            if tree is None:
                continue
            held = readings(tree, vocabulary_of(store, name))
            found = sweep(store.load_state(name), held)
            yield WorkSummary(
                id=name,
                title=tree.title,
                parts=len(held),
                outstanding=len(found.dirty()),
            )

    return list(summarised())


@router.get("/in-flight")
def everything_in_flight() -> list[PartInFlight]:
    """Every part of every work being written right now, longest-running first.

    One page for the question an author with two books open actually has —
    what is being worked on — rather than one page per work to be checked in
    turn. Declared above ``/{work}`` so the literal path wins the match; a
    work slugged ``in-flight`` would be unreachable, which is a better trade
    than this being a work's tree.
    """
    store = manuscript_store()

    def everywhere() -> Iterator[PartInFlight]:
        """Each work's in-flight parts, works with none contributing nothing."""
        for name in store.works():
            if store.load_tree(name) is not None:
                yield from in_flight(store, name)

    return sorted(everywhere(), key=lambda held: held.since)


@router.post("", status_code=201)
def record_work(request: ImportRequest) -> WorkImport:
    """Record a work, so the browser is not a reader of what a terminal set up.

    The same procedure the command line runs, called rather than restated. The
    directory is read on the server, because that is where the checkout is and
    where every run against it will look.
    """
    chapters = Path(request.chapters).expanduser()
    if not chapters.is_dir():
        raise HTTPException(
            status_code=422, detail=f"No such directory on the server: {chapters}"
        )
    store = manuscript_store()
    refusal = unknown_format(request.target_format)
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)
    declared = Path(request.vocabulary).expanduser() if request.vocabulary else None
    with claim_mutation(store, request.work):
        return import_work(
            store,
            request.work,
            chapters,
            title=request.title,
            target_format=request.target_format,
            vocabulary=declared,
            adopt=request.adopt,
        )


@router.get("/{work}")
def work_tree(work: str) -> WorkTree:
    """The whole work with every part's state, which is what the tree renders."""
    store = manuscript_store()
    tree = tree_of(store, work)
    state = store.load_state(work)
    found = sweep(state, readings(tree, vocabulary_of(store, work)))
    judged = {verdict.key: verdict for verdict in found.verdicts}
    waiting = PartMailbox(root=store.work_dir(work)).open()

    def rendered() -> Iterator[PartNode]:
        """Each part of the work, with whatever is known about it."""
        for node in tree.walk():
            yield part_node(tree, state, judged, waiting, node)

    return WorkTree(
        id=work,
        title=tree.title,
        root=tree.root,
        target_format=tree.target_format,
        rooted=missing_root(tree) is None,
        nodes=tuple(rendered()),
        outstanding=len(found.dirty()),
        blocked=len(found.blocked()),
        settled=found.settled(),
        loop=loop_status(store, work),
        in_flight=in_flight(store, work),
    )


def standing_of(handle: SessionHandle | None, looping: bool) -> FlightStanding:
    """What a leased part is actually doing, in one word somebody can act on.

    Three cases and they need different doors: a run is publishing through its
    handle and can be stopped; a pass holds the lease but this part's turn has
    not come, so there is nothing to stop and nothing wrong; or nobody is
    running it at all and the lease wants clearing.
    """
    if handle is not None:
        return handle.status
    return "queued" if looping else "orphaned"


def in_flight(store: ManuscriptStore, work: str) -> tuple[PartInFlight, ...]:
    """Every part of one work being written right now, with what its run says.

    Read off the work's own state rather than off the loop, because the state
    is what the lease is recorded in and it outlives the process: a part left
    held by a run that is gone reads as ``orphaned`` here, which is the case
    worth surfacing rather than the one worth hiding — it looks exactly like
    work in progress and will never finish on its own.

    Whether a lease is honoured is answered by the loop and not by the session,
    because a pass leases every part it means to run before running any of
    them: a part waiting its turn behind the concurrency cap has no run yet and
    is not lost, and calling it orphaned would send somebody to clear a lease
    that is about to be used.
    """
    tree = tree_of(store, work)
    state = store.load_state(work)
    sessions = holder.loops.sessions if holder.loops else None
    looping = loop_running(store, work)

    def held() -> Iterator[PartInFlight]:
        """One entry per part a run has taken and not yet given back."""
        for node in tree.walk():
            record = state.record(node.key)
            if record is None or record.standing != "running":
                continue
            handle = sessions.handle_for(record.holder) if sessions else None
            yield PartInFlight(
                work=work,
                key=node.key,
                title=node.title,
                session=record.holder,
                since=record.changed_at,
                reason=record.reason,
                stage=handle.state.stage if handle else "",
                status=standing_of(handle, looping),
                cost_usd=handle.cost.total.cost_usd if handle else 0.0,
                doc_url=handle.state.doc_url if handle else "",
            )

    return tuple(held())


def would_run(
    store: ManuscriptStore, work: str, limit: int, only: tuple[str, ...] = ()
) -> WouldRun:
    """What a pass over this work would pick up, spending nothing to find out."""
    tree = tree_of(store, work)
    state = store.load_state(work)
    found = sweep(state, readings(tree, vocabulary_of(store, work)))
    outstanding = narrowed(schedulable(found, state), only)
    return WouldRun(
        work=work,
        parts=outstanding[:limit] if limit else outstanding,
        outstanding=len(outstanding),
        blocked=found.blocked(),
        passed_over=unpicked(found, state, only),
    )


@router.get("/{work}/would-run")
def preview_run(
    work: str,
    limit: int = 0,
    part: Annotated[list[str] | None, Query()] = None,
) -> WouldRun:
    """What running this work would do, before anybody agrees to pay for it.

    Every part a pass picks up is a whole pipeline run, so this is the question
    that comes before the button: a browser offering only *start* would be
    offering to spend an unknown amount on one click.

    ``part`` asks the same question about one subsection, which is what a row in
    the tree needs before it can offer to run just itself.
    """
    return would_run(manuscript_store(), work, limit, tuple(part or ()))


@router.post("/{work}/run", status_code=202)
async def start_run(work: str, request: RunRequest) -> WorkLoopStatus:
    """Take this work to rest in the background, and say where that stands.

    Refused where the work's source is gone: the loop would schedule nothing and
    report a settled book, and the actual answer is about a checkout.

    Awaitable because the loop it starts is a task, and a task needs a loop to
    belong to: a synchronous endpoint is handed to a worker thread, where there
    is no running event loop and the pass cannot be scheduled at all.
    """
    store = manuscript_store()
    tree = tree_of(store, work)
    gone = missing_root(tree)
    if gone is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{work} was imported from {gone}, which is not there — "
            "restore that checkout or re-import the work",
        )
    try:
        return get_loops().start(
            store,
            work,
            tree,
            vocabulary=vocabulary_of(store, work),
            passes=request.passes,
            limit=request.limit,
            only=request.parts,
            concurrency=request.concurrency,
            reconciling=request.reconcile,
        )
    except LoopAlreadyRunning as already:
        raise HTTPException(status_code=409, detail=str(already)) from already


@router.get("/{work}/run")
def run_status(work: str) -> WorkLoopStatus:
    """Where the loop over this work stands, pass by pass."""
    store = manuscript_store()
    tree_of(store, work)
    return loop_status(store, work) or WorkLoopStatus(work=work, running=False)


@router.post("/{work}/run/stop")
async def stop_run(work: str) -> WorkLoopStatus:
    """Stop the loop over this work, once the part in flight has finished writing.

    The parts it had leased stay ``running`` with the run that held them named,
    rather than being reclaimed here — a lease nobody is honouring is something
    a person should look at, and ``clear`` is how they release it.
    """
    tree_of(manuscript_store(), work)
    return await get_loops().stop(work)


@router.post("/{work}/parts/{key:path}/run", status_code=202)
async def start_part_run(
    work: str, key: str, request: RunPartRequest
) -> WorkLoopStatus:
    """Atomically select, prepare, and run one part.

    The work is claimed before an up-to-date part is marked requested. That
    keeps the button one operation: losing a race to another surface cannot
    leave a request behind that the user never meant to queue.
    """
    store = manuscript_store()
    tree = tree_of(store, work)
    gone = missing_root(tree)
    if gone is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{work} was imported from {gone}, which is not there — "
            "restore that checkout or re-import the work",
        )
    manager = get_loops()
    try:
        lease = manager.claim(store, work)
    except LoopAlreadyRunning as already:
        raise HTTPException(status_code=409, detail=str(already)) from already

    opening: WorkState | None = None
    changed = False
    try:
        selected = one_part(store, work, key)
        if not selected.leaf:
            raise HTTPException(
                status_code=422,
                detail=f"{key} contains parts and cannot itself be run",
            )
        if selected.staleness == "source-gone":
            raise HTTPException(
                status_code=409,
                detail=f"{key} has no source text at {selected.path}",
            )
        if selected.standing != "idle":
            raise HTTPException(
                status_code=409,
                detail=f"{key} is {selected.standing} and cannot be started",
            )

        opening = store.load_state(work)
        changed = bool(request.reason) or selected.staleness == "fresh"
        if changed:
            store.publish_state(
                work, opening.declared(key, "requested", request.reason)
            )

        preview = would_run(store, work, 0, (key,))
        if preview.passed_over:
            skipped = preview.passed_over[0]
            raise HTTPException(
                status_code=409,
                detail=f"{skipped.key} is not being run: {skipped.reason}",
            )
        if not preview.parts:
            raise HTTPException(status_code=409, detail=f"{key} would not run")

        return manager.start(
            store,
            work,
            tree,
            vocabulary=vocabulary_of(store, work),
            passes=1,
            limit=0,
            only=(key,),
            concurrency=1,
            reconciling=True,
            lease=lease,
        )
    except Exception:
        if changed and opening is not None:
            store.publish_state(work, opening)
        lease.release()
        raise


@router.get("/{work}/questions")
def open_questions(work: str) -> list[QuestionView]:
    """What the work's parts are waiting on."""
    store = manuscript_store()
    tree_of(store, work)
    return [
        QuestionView(
            id=held.id,
            asker=held.asker,
            addressed_to=held.addressed_to,
            prompt=held.prompt,
        )
        for held in PartMailbox(root=store.work_dir(work)).open()
    ]


def released(
    store: ManuscriptStore,
    work: str,
    mailbox: PartMailbox,
    asked: PartQuestion,
    value: str,
) -> None:
    """Let a part run again, once nothing else it asked is still waiting.

    A part that asked three questions is not ready when the first is
    answered: it would run again and park on the other two, having spent a
    whole pipeline run to re-ask what it already asked.
    """
    if mailbox.asked_by(asked.asker):
        return
    state = store.load_state(work)
    store.publish_state(
        work, state.declared(asked.asker, "requested", f"answered: {value}")
    )


@router.post("/{work}/questions/{question}/answer")
def answer_question(work: str, question: str, request: AnswerRequest) -> QuestionView:
    """Answer a parked question, which is what lets its part run again."""
    store = manuscript_store()
    tree_of(store, work)
    mailbox = PartMailbox(root=store.work_dir(work))
    with claim_mutation(store, work):
        asked = next((held for held in mailbox.open() if held.id == question), None)
        if asked is None:
            raise HTTPException(
                status_code=404,
                detail=f"No question of {work} is waiting under {question!r}",
            )
        if not mailbox.answer(
            PartAnswer(
                id=question,
                value=request.value,
                answered_by=request.answered_by,
            )
        ):
            raise HTTPException(
                status_code=409, detail=f"{question} was already answered"
            )
        released(store, work, mailbox, asked, request.value)
    return QuestionView(
        id=asked.id,
        asker=asked.asker,
        addressed_to=asked.addressed_to,
        prompt=asked.prompt,
    )


@router.post("/{work}/parts/{key:path}/request")
def request_part(work: str, key: str, request: RequestRevision) -> PartNode:
    """Ask for one part to be revised, which is what makes it outstanding.

    Refused while a loop owns the work. The loop is the single state writer,
    so even asking for a different part waits until the current pass has
    published everything it learned rather than racing that publication.
    """
    store = manuscript_store()
    tree = tree_of(store, work)
    if tree.node(key) is None:
        raise HTTPException(status_code=404, detail=f"{work} has no part {key!r}")
    with claim_mutation(store, work):
        running = store.load_state(work).record(key)
        if running is not None and running.standing == "running":
            raise HTTPException(
                status_code=409,
                detail=f"{key} is being run right now by {running.holder} — "
                "wait for that run, or stop the work's loop first",
            )
        store.publish_state(
            work, store.load_state(work).declared(key, "requested", request.reason)
        )
    return one_part(store, work, key)


@router.post("/{work}/parts/{key:path}/clear")
def clear_part(work: str, key: str) -> PartNode:
    """Take back what was said about one part, returning it to idle.

    The way out of the two standings that are deliberately sticky: a failed part
    stays failed so a pass cannot pick it up and fail the same way forever, and a
    requested one stays requested until something rewrites it. Both are right,
    and both need a door — this is somebody saying they have read the failure, or
    changed their mind.

    Refused while a loop is running, because a part it is holding would be
    cleared and picked up again by the pass that already has it.
    """
    store = manuscript_store()
    tree = tree_of(store, work)
    if tree.node(key) is None:
        raise HTTPException(status_code=404, detail=f"{work} has no part {key!r}")
    with claim_mutation(store, work):
        store.publish_state(work, store.load_state(work).declared(key, "idle", ""))
    return one_part(store, work, key)


@router.get("/{work}/reaches")
def reaches(work: str, subject: str, kind: str = "term") -> ReachedBy:
    """Which parts would go out of date if this changed.

    The question an author has before making a change rather than after, and
    the one a tree is the right surface for: the answer is a set of parts to
    look at.
    """
    store = manuscript_store()
    tree = tree_of(store, work)
    if kind not in FACT_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"{kind!r} is no kind of dependency: {', '.join(FACT_KINDS)}",
        )
    held = readings(tree, vocabulary_of(store, work))
    leaning = consumers(held, Dependency(kind=kind, subject=subject))
    return ReachedBy(
        kind=kind, subject=subject, parts=tuple(node.key for node in leaning)
    )


class RunningAgent(BaseModel):
    """One agent inside a part's run, as something surveying the pass reads.

    The third level, and the one nothing outside a run could see. A part run
    reports which subsection it holds and how far through the pipeline it is;
    beneath that, nine writers and six reviewers were doing the work and
    reporting nowhere. They were on the cohort's disk roster the whole time.
    """

    model_config = ConfigDict(frozen=True)

    address: str = Field(description="What reaches this agent")
    kind: str = Field(description="What it is — writer, reviewer, judge")
    task: str = Field(default="", description="What it was asked")
    running: bool = Field(description="Whether it is still working")
    summary: str = Field(default="", description="What it said, once it had")
    error: str = Field(default="", description="What went wrong, where it did")


class WorkingAgent(BaseModel):
    """One agent working on a work rather than on any part of it.

    Distilling a document, placing findings, checking a reference. A pass that
    spends two hundred dollars reading before it writes a word is doing the
    most expensive thing it will do all day, and with nothing reporting it the
    work looks idle — which is the state somebody stops a loop out of.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="What this agent is addressed by")
    kind: str = Field(description="What it is — distil, assign, reference, brief")
    about: str = Field(default="", description="What it is reading or placing")
    running: bool = Field(description="Whether it is still working")
    opened_at: datetime = Field(description="When it started")
    detail: str = Field(default="", description="What it found, once it has")
    failure: str = Field(default="", description="Why it produced nothing")


class WorkActivity(BaseModel):
    """Everything happening to one work, at all three of its levels.

    One reply rather than three, because the question is one question: what is
    this work doing. Split across three calls, a page would render a pass with
    no parts and then parts with no agents, and the moment somebody is asking
    about is the moment those disagree.
    """

    work: str = Field(description="The work")
    loop: WorkLoopStatus | None = Field(
        default=None, description="The pass, where one is running or has run"
    )
    parts: tuple[PartInFlight, ...] = Field(
        default=(), description="Every part a run is holding"
    )
    agents: dict[str, tuple[RunningAgent, ...]] = Field(
        default={},
        description="The agents inside each part's run, by part key",
    )
    working: tuple[WorkingAgent, ...] = Field(
        default=(),
        description="The agents belonging to no part — the ones a pass looks "
        "idle without",
    )

    def busy(self) -> bool:
        """Whether anything at all is happening, at any of the three levels."""
        return bool(
            self.parts
            or any(one.running for one in self.working)
            or (self.loop is not None and self.loop.running)
        )


def agents_of(
    session: str, sessions: SessionManager | None = None
) -> tuple[RunningAgent, ...]:
    """Every agent in one part, from live events plus the durable cohort roster."""
    handle = sessions.handle_for(session) if sessions is not None else None

    def live_updates() -> Iterator[AgentUpdate]:
        for event in handle.events if handle is not None else ():
            try:
                yield AgentEvent.model_validate(event.model_dump()).agent
            except ValidationError:
                continue

    updates = tuple({update.id: update for update in live_updates()}.values())
    observed = tuple(
        RunningAgent(
            address=update.address,
            kind=update.label,
            task=update.model or "",
            running=update.status == "running",
            summary=update.status if update.status == "completed" else "",
            error=update.error,
        )
        for update in updates
    )
    addresses = {one.address for one in observed}
    cohort = tuple(
        RunningAgent(
            address=held.address,
            kind=held.actor.kind,
            task=held.task,
            running=held.running,
            summary=held.summary,
            error=held.error,
        )
        for held in roster_of(session)
        if held.address not in addresses
    )
    return (*observed, *cohort)


def working_agents(store: ManuscriptStore, work: str) -> tuple[WorkingAgent, ...]:
    """Every agent this work has spent outside its parts, newest first."""
    return tuple(
        WorkingAgent(
            id=held.id,
            kind=held.kind,
            about=held.about,
            running=held.running(),
            opened_at=held.opened_at,
            detail=held.detail,
            failure=held.failure,
        )
        for held in reversed(store.attending(work).load().agents)
    )


@router.get("/{work}/activity")
def activity(work: str) -> WorkActivity:
    """What this work is doing, from the pass down to the agents.

    The three levels joined where they are asked about together. A part's
    agents come from the cohort roster its run keeps on disk, so this reads
    what the run wrote rather than asking the run — which is what makes it
    answerable from a browser in another process, and from a page reloaded
    after the run that started it went away.
    """
    store = manuscript_store()
    tree_of(store, work)
    held = in_flight(store, work)
    return WorkActivity(
        work=work,
        loop=loop_status(store, work),
        parts=held,
        agents={
            one.key: agents_of(one.session, holder.session_manager())
            for one in held
            if one.session
        },
        working=working_agents(store, work),
    )


class PartHistory(BaseModel):
    """One run against one part, and how far it got.

    Read off the run's own room rather than off the build stamps, which is
    what makes it symmetric: a stamp exists only where a run produced prose, so
    a history read from stamps is a history of successes with every failure
    missing — and the failures are the runs most worth opening.
    """

    model_config = ConfigDict(frozen=True)

    session: str = Field(description="The run")
    key: str = Field(description="The part it was about")
    title: str = Field(default="", description="How the work names that part")
    opened_at: datetime = Field(description="When it started")
    reasons: tuple[str, ...] = Field(
        default=(), description="Why the part was picked up"
    )
    produced: bool = Field(
        default=False, description="Whether the pipeline came back with prose"
    )
    adopted: bool = Field(
        default=False, description="Whether the inheritance pass settled a successor"
    )
    built: bool = Field(
        default=False, description="Whether this is the run the part was built from"
    )


@router.get("/{work}/history")
def history(work: str, key: str = "") -> list[PartHistory]:
    """Every run this work has recorded, newest first.

    ``key`` narrows it to one part, which is the question somebody usually has:
    this subsection has been run four times and reads the same, so what did
    each of them do. How far a run got is which files its room holds, so a run
    that produced nothing says so rather than being absent.
    """
    store = manuscript_store()
    tree_of(store, work)
    state = store.load_state(work)
    room = store.work_dir(work) / "runs"

    def held() -> Iterator[PartHistory]:
        """Each recorded run, narrowed to the part named where one is."""
        for run in runs_under(room):
            if key and run.key != key:
                continue
            stamp = state.stamp(run.key)
            yield PartHistory(
                session=run.session,
                key=run.key,
                title=run.title,
                opened_at=run.opened_at,
                reasons=run.reasons,
                produced=(room / run.session / PRODUCED_FILE).is_file(),
                adopted=(room / run.session / ADOPTED_FILE).is_file(),
                built=stamp is not None and stamp.run == run.session,
            )

    return list(held())
