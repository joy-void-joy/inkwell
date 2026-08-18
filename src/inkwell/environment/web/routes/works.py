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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from inkwell.agent.config import manuscript_store
from inkwell.agent.glossary import DECLARED_VOCABULARY, load_chapter_glossary
from inkwell.manuscript.facts import FACT_KINDS, Dependency
from inkwell.manuscript.graph import consumers, readings, sweep
from inkwell.manuscript.mailbox import PartAnswer, PartMailbox, PartQuestion
from inkwell.manuscript.state import NodeStanding, NodeVerdict, Staleness
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript
from inkwell.manuscript.vocabulary import Abbreviation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/works", tags=["works"])


class WorkSummary(BaseModel):
    """One work as a listing shows it."""

    id: str = Field(description="The slug the work is recorded under")
    title: str = Field(description="What the work is called")
    parts: int = Field(description="How many parts a run can be about")
    outstanding: int = Field(description="How many of them have work left")


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


class WorkTree(BaseModel):
    """A whole work, its state, and what it is waiting on."""

    id: str = Field(description="The work")
    title: str = Field(description="What it is called")
    nodes: tuple[PartNode, ...] = Field(
        default=(), description="Every part, in reading order"
    )
    outstanding: int = Field(default=0, description="Parts with work left")
    settled: bool = Field(default=True, description="Whether the work is at rest")


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


def parent_of(tree: Manuscript, key: str) -> str:
    """The nearest ancestor of a key among the work's own parts."""
    above = [
        node.key
        for node in tree.walk()
        if key.startswith(f"{node.key}/") and node.key != key
    ]
    return max(above, key=len) if above else ""


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
        """Each part of the work, with whatever is known about it.

        A part above the leaves has no verdict of its own — nothing runs
        against it — and reads as fresh, which is what it is: a chapter is
        never out of date, the parts under it are.
        """
        for node in tree.walk():
            verdict = judged[node.key] if node.key in judged else None
            yield PartNode(
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
            )

    return WorkTree(
        id=work,
        title=tree.title,
        nodes=tuple(rendered()),
        outstanding=len(found.dirty()),
        settled=found.settled(),
    )


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
    asked = next((held for held in mailbox.open() if held.id == question), None)
    if asked is None:
        raise HTTPException(
            status_code=404,
            detail=f"No question of {work} is waiting under {question!r}",
        )
    if not mailbox.answer(
        PartAnswer(id=question, value=request.value, answered_by=request.answered_by)
    ):
        raise HTTPException(status_code=409, detail=f"{question} was already answered")
    released(store, work, mailbox, asked, request.value)
    return QuestionView(
        id=asked.id,
        asker=asked.asker,
        addressed_to=asked.addressed_to,
        prompt=asked.prompt,
    )


@router.post("/{work}/parts/{key:path}/request")
def request_part(work: str, key: str, request: RequestRevision) -> PartNode:
    """Ask for one part to be revised, which is what makes it outstanding."""
    store = manuscript_store()
    tree = tree_of(store, work)
    node = tree.node(key)
    if node is None:
        raise HTTPException(status_code=404, detail=f"{work} has no part {key!r}")
    state = store.load_state(work).declared(key, "requested", request.reason)
    store.publish_state(work, state)
    found = sweep(state, readings(tree, vocabulary_of(store, work)))
    verdict = next(
        (held for held in found.verdicts if held.key == key),
        NodeVerdict(key=key, staleness="requested"),
    )
    return PartNode(
        key=key,
        title=node.title,
        kind=node.kind,
        parent=parent_of(tree, key),
        depth=key.count("/"),
        path=node.path,
        leaf=not node.children,
        staleness=verdict.staleness,
        standing=state.standing(key),
        reasons=verdict.reasons,
    )


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
