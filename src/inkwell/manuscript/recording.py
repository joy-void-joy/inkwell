"""Recording a work into the store, once, for every surface that asks for it.

Reading a work is :mod:`.ingest`; judging one is :mod:`.graph`; keeping one is
:mod:`.store`. Importing is all three in a fixed order — read the tree, write
the vocabulary the authors declared, stamp what already exists as built — and
that order is the part worth having in one place. There is more than one way to
ask for an import: a command line and a browser both do, and an import that
adopted on one surface but not the other would leave the two in different states
while both claimed to have imported the same book.

Above ``graph`` on purpose. ``ingest`` is the reader that ``links`` and
``splice`` are built on, so it cannot reach back up to the dependency pass; this
sits where it can see all of them.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.glossary import DECLARED_VOCABULARY, write_chapter_glossary
from inkwell.manuscript.graph import adopted, readings
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import DEFAULT_WORK_FORMAT, DEFAULT_WRITER_MODE
from inkwell.manuscript.vocabulary import (
    Abbreviation,
    as_glossary,
    read_abbreviations,
)

logger = logging.getLogger(__name__)

ABBREVIATIONS_PATH = Path("includes/abbreviations.md")
"""Where a work declares its abbreviations, relative to the chapters' parent.

The mkdocs convention the Atlas follows, and a fact about how a work is laid
out rather than about any one way of asking for an import — which is why it sits
here rather than at whichever surface happened to need it first.
"""


class WorkImport(BaseModel):
    """What recording a work established, for a caller to render or return.

    Counted rather than described, because both surfaces that ask want the same
    few numbers and neither wants the tree back — the store has it, and a reply
    carrying two hundred nodes to say an import worked would be answering a
    different question.
    """

    model_config = ConfigDict(frozen=True)

    work: str = Field(description="The slug it was recorded under")
    title: str = Field(description="What the work is called")
    root: str = Field(description="Where its chapters were read from")
    target_format: str = Field(description="What every part of it is written as")
    writer_mode: str = Field(
        default="",
        description="How each part is drafted — one writer, or one per section",
    )
    skipped_stages: tuple[str, ...] = Field(
        default=(), description="Stages a part run of this work does not perform"
    )
    parts: int = Field(description="Leaf parts a run can be about")
    vocabulary: int = Field(description="Abbreviations the work declares")
    dependencies: int = Field(description="Edges between parts across the work")
    adopted: int = Field(description="Parts now stamped as built from what they hold")


def import_work(
    store: ManuscriptStore,
    work: str,
    chapters: Path,
    *,
    title: str = "",
    target_format: str = DEFAULT_WORK_FORMAT,
    writer_mode: str = DEFAULT_WRITER_MODE,
    skipped_stages: tuple[str, ...] = (),
    vocabulary: Path | None = None,
    adopt: bool = True,
) -> WorkImport:
    """Record a work so runs months apart share one account of it.

    Adopting stamps every part as built from the text it already holds, which is
    the difference between a loop usable today and one that must first rewrite
    two hundred parts. Turning it off treats every part as unwritten, which is
    what a work being drafted from nothing actually wants.

    Re-importing keeps everything anybody asked of the work: the tree is a cache
    and is rewritten, the state is the record and is loaded before it is
    written, so a part already stamped keeps the stamp its run left rather than
    being re-adopted from whatever the file says now.
    """
    tree = read_manuscript(
        chapters,
        title=title,
        target_format=target_format,
        writer_mode=writer_mode,
        skipped_stages=skipped_stages,
    )
    store.publish_tree(work, tree)

    abbreviations_at = (
        vocabulary if vocabulary is not None else chapters.parent / ABBREVIATIONS_PATH
    )
    abbreviations: tuple[Abbreviation, ...] = read_abbreviations(abbreviations_at)
    if abbreviations:
        vocabulary_path = store.glossary_path(work, DECLARED_VOCABULARY)
        vocabulary_path.parent.mkdir(parents=True, exist_ok=True)
        write_chapter_glossary(vocabulary_path, as_glossary(abbreviations))

    held = readings(tree, abbreviations)
    state = store.load_state(work)
    if adopt:
        state = adopted(state, held)
    store.publish_state(work, state)

    logger.info("Recorded %s: %d part(s) from %s", work, len(held), tree.root)
    return WorkImport(
        work=work,
        title=tree.title,
        root=tree.root,
        target_format=tree.target_format,
        writer_mode=tree.writer_mode,
        skipped_stages=tree.skipped_stages,
        parts=len(held),
        vocabulary=len(abbreviations),
        dependencies=sum(len(reading.consumed.dependencies) for reading in held),
        adopted=len(state.stamps),
    )
