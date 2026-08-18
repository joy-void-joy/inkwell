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
megabyte. The cost is that the run's writers coin into their own run glossary
rather than straight into the work's, so what they coined is carried across
afterwards; the seam is named in :func:`harvested` rather than hidden.

**The lease is the standing.** A part is marked ``running`` with the session
holding it before anything starts, and a part already held is one the loop
will not schedule twice. A run that fails leaves ``failed`` with what it said,
rather than leaving the part looking untouched — a part that silently reverted
to idle would be picked up again on the next pass and fail the same way.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.core import run_session
from inkwell.agent.glossary import (
    ChapterGlossary,
    GlossaryEntry,
    load_chapter_glossary,
    write_chapter_glossary,
)
from inkwell.manuscript.facts import (
    Consumption,
    Dependency,
    ProposedChange,
    consumption_of,
)
from inkwell.manuscript.links import links_from
from inkwell.manuscript.splice import PartNotFound, held_text, spliced
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.manuscript.vocabulary import Abbreviation, terms_used

logger = logging.getLogger(__name__)

REVISION_FILE = "part.md"
"""What a part's text is called where a run is handed it.

A file rather than the text inline, because ``revision_target`` takes sources
the extract stage opens, and a part of a book is exactly a document.
"""


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

    def succeeded(self) -> bool:
        """Whether there is prose here to put back into the work."""
        return not self.failure and bool(self.text)


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
    return (
        f"You are revising one part of a larger work: {placed(manuscript, node)}.\n\n"
        "Keep the author's structure, voice, and argument. This part sits among "
        "others that readers reach before and after it, so do not reintroduce "
        "what the work has already established, and do not rename anything the "
        "shared glossary already settles — look it up and adopt it. Revise for "
        "clarity and for currency, questioning the part's own claims where they "
        "have dated. Return this part alone, opening with its own heading "
        "exactly as it stands." + because
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


def harvested(
    store: ManuscriptStore, work: str, key: str, run_glossary: Path
) -> tuple[ProposedChange, ...]:
    """Carry what a run coined into the work's ledger, and say what is new.

    The seam this module's outside-composition costs. A run's writers coin
    into the run's own glossary; this moves those terms into the part's file
    under the work, where the next part to run will read them.

    First definition still wins, and the work's existing terms are what it
    wins against: a term the work already has is not re-coined and publishes
    no change, so a run that reached for an established name moves nothing.
    Only a genuinely new coinage is a change other parts could care about.
    """
    coined = load_chapter_glossary(run_glossary).terms
    if not coined:
        return ()
    known = {
        entry.term.casefold()
        for path in sorted(store.glossary_dir(work).glob("*.json"))
        for entry in load_chapter_glossary(path).terms
    }
    fresh = [entry for entry in coined if entry.term.casefold() not in known]
    if not fresh:
        return ()
    own = store.glossary_path(work, key)
    own.parent.mkdir(parents=True, exist_ok=True)
    held = load_chapter_glossary(own)
    write_chapter_glossary(
        own,
        ChapterGlossary(
            run=key,
            conventions=held.conventions,
            terms=[
                *held.terms,
                *[
                    GlossaryEntry(
                        term=entry.term, meaning=entry.meaning, aliases=entry.aliases
                    )
                    for entry in fresh
                ],
            ],
        ),
    )
    return tuple(
        ProposedChange(
            dependency=Dependency(kind="term", subject=entry.term),
            detail=f"coined while revising {key}",
        )
        for entry in fresh
    )


def written_back(manuscript: Manuscript, node: ManuscriptNode, text: str) -> Path:
    """Put one part's prose back where it came from, disturbing nothing else.

    A part that owns its file has the file replaced; a part that shares one has
    its own span replaced and its siblings left byte-identical. Raises where
    the part's heading is no longer in the file, because the alternative is a
    rewrite that goes nowhere and a run that reports success anyway.
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
) -> PartOutcome:
    """Take one part through the pipeline and hand back what it produced.

    Writes the part's prose back into the work, because that is what the next
    part to read it must see; does not touch the work's state, because the
    loop owns that and needs the outcome in hand before deciding anything.
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

    result = await run_session(
        sources=[str(material), part_instruction(manuscript, node, reasons)],
        material_role="revision_target",
        session_id=session_id,
    )
    produced = result.output.content if result.output else ""
    if not produced:
        return PartOutcome(
            key=node.key,
            session=session_id,
            failure="the run finished without producing any prose",
        )

    try:
        written_back(manuscript, node, produced)
    except (PartNotFound, OSError) as failure:
        logger.exception("Could not put %s back into %s", node.key, node.path)
        return PartOutcome(key=node.key, session=session_id, failure=str(failure))

    coined = harvested(store, work, node.key, room / "glossary.json")
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
