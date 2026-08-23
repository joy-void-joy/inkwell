"""Bringing what the corpus knows to bear on a work, as one step of the build.

Three arrows, run in order and each cached on what it is about:

    corpus doc ──distil──▶ finding ──assign──▶ bearing ──▶ change fact
             content_sha256        findings + tree        the ledger

Nothing here reads the book's prose or spends a writing run. What it produces
is entries in the same change ledger a rewrite produces, so the sweep dirties
the parts the research reached through exactly the mechanism it already uses,
and the loop picks them up with a reason naming the paper.

**Priced before it is spent.** Distilling is the expensive arrow — a
whole-document reading per document the filter reaches and nothing has read
yet — and it is knowable in advance, because "has this content been read" is a
file that exists or does not. So a sync can be asked what it would cost without
costing it, which is what makes a retag safe to consider: re-tagging changes
which documents a filter reaches, which changes what lands on parts, which
dirties them, and none of that should happen because somebody ran a command to
see what would happen.

**Publishing is the separate step.** Reading and placing move nothing; only
appending the change facts does. Keeping them apart is what lets a sync report
"this would dirty 34 parts" and wait, rather than reporting it afterwards.
"""

import logging
from collections.abc import Callable, Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from inkwell.corpus.distillation import (
    DEFAULT_DISTIL_CONCURRENCY,
    DistilReport,
    DistilStep,
    Distiller,
    awaiting_distillation,
    distil_entries,
    findings_for,
)
from inkwell.corpus.retrieval import CorpusEntry, CorpusFilter, read_index
from inkwell.corpus.storage import CorpusStore
from inkwell.corpus.tags import TagVocabulary
from inkwell.manuscript.findings import (
    CORPUS_ORIGIN,
    Bearing,
    WorkFindings,
    arrived,
    assign,
    changes,
)
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript

logger = logging.getLogger(__name__)


class ResearchPlan(BaseModel):
    """What a sync would read, before it reads any of it.

    The answer to "what does this cost", which has to exist separately from the
    doing: an operator deciding whether to re-tag a corpus is deciding about a
    number, and getting the number by running the thing would be getting it by
    paying for it.
    """

    model_config = ConfigDict(frozen=True)

    reached: int = Field(default=0, description="Documents the work's filter reaches")
    unread: int = Field(
        default=0, description="Of those, ones nothing has distilled yet"
    )
    vocabulary: str = Field(
        default="", description="Signature of the tag vocabulary it would run under"
    )
    retagged: bool = Field(
        default=False,
        description="Whether the corpus has been re-tagged since the standing "
        "assignment was made, so the filter now reaches a different set",
    )

    def render(self) -> str:
        """This plan as the sentence an operator decides on."""
        moved = (
            "\nThe tag vocabulary has changed since the standing assignment, so "
            "this filter reaches a different set of documents than it did."
            if self.retagged
            else ""
        )
        return (
            f"{self.reached} document(s) reached, {self.unread} of them not yet "
            f"read. Reading one document is one delegated session over the whole "
            f"of it.{moved}"
        )


class ResearchSync(BaseModel):
    """What one sync read, placed, and would dirty — before anything is written.

    Held together because the decision is about all three at once: an operator
    is asked whether 34 parts going out of date is worth it, and the answer
    depends on what the findings are.
    """

    work: str = Field(description="The work this is about")
    read: DistilReport = Field(
        default_factory=DistilReport, description="What the distilling did"
    )
    found: WorkFindings = Field(
        default_factory=WorkFindings, description="Where everything was placed"
    )
    arrivals: tuple[Bearing, ...] = Field(
        default=(), description="What is new since the standing assignment"
    )

    def dirtied(self) -> tuple[str, ...]:
        """Every part these arrivals would put out of date, each once."""
        return tuple(dict.fromkeys(one.key for one in self.arrivals))

    def render(self) -> str:
        """This sync as the line somebody deciding whether to publish reads."""
        parts = self.dirtied()
        return (
            f"{self.read.summary()}\n"
            f"placed {len(self.found.bearings)} finding(s); "
            f"{len(self.arrivals)} new, on {len(parts)} part(s)"
        )

    def detail(self) -> Iterator[str]:
        """Each part that would go out of date, and what would do it."""
        for key in self.dirtied():
            arrivals = [one for one in self.arrivals if one.key == key]
            yield f"{key} — {len(arrivals)} new finding(s)"
            for one in arrivals:
                yield f"    {one.render()}"


def reaching(
    corpus: CorpusStore, where: CorpusFilter, *, sources: tuple[str, ...] = ()
) -> tuple[CorpusEntry, ...]:
    """The documents a work's filter reaches, as entries to read or to price.

    A filter rather than the whole corpus, because most of the corpus is not
    about what any one book covers: filtering by the tag vocabulary takes the
    Atlas's cyber subsection from eleven hundred documents to a few hundred,
    and the tags are already there and already versioned.
    """
    return where.apply(read_index(corpus, sources).entries)


def plan(
    corpus: CorpusStore,
    store: ManuscriptStore,
    work: str,
    where: CorpusFilter,
    vocabulary: TagVocabulary,
    *,
    sources: tuple[str, ...] = (),
) -> ResearchPlan:
    """What syncing this work's research would read, without reading any of it."""
    entries = reaching(corpus, where, sources=sources)
    signature = vocabulary.signature()
    standing = store.load_findings(work)
    return ResearchPlan(
        reached=len(entries),
        unread=len(awaiting_distillation(corpus, entries)),
        vocabulary=signature,
        retagged=bool(standing.vocabulary) and standing.vocabulary != signature,
    )


async def sync(
    corpus: CorpusStore,
    store: ManuscriptStore,
    work: str,
    manuscript: Manuscript,
    where: CorpusFilter,
    vocabulary: TagVocabulary,
    *,
    sources: tuple[str, ...] = (),
    distiller: Distiller | None = None,
    concurrency: int = DEFAULT_DISTIL_CONCURRENCY,
    progress: Callable[[DistilStep], None] | None = None,
) -> ResearchSync:
    """Read what the filter reaches, place it, and say what it would dirty.

    Writes nothing about the work. The distillations are written, because they
    are keyed on content and are true whatever anybody decides next; the
    assignment and the change facts are returned, because publishing them is
    the step that puts parts out of date and that decision is not this
    function's.
    """
    entries = reaching(corpus, where, sources=sources)
    read = await distil_entries(
        corpus, entries, distiller, concurrency=concurrency, progress=progress
    )
    found = await assign(
        manuscript, findings_for(corpus, entries), vocabulary=vocabulary.signature()
    )
    arrivals = arrived(store.load_findings(work), found)
    logger.info("Research sync of %s: %s", work, read.summary())
    return ResearchSync(work=work, read=read, found=found, arrivals=arrivals)


def publish(store: ManuscriptStore, synced: ResearchSync) -> tuple[str, ...]:
    """Record where the research landed and dirty the parts it reached.

    Both together and in this order, because they are one decision: the
    assignment is what the next sync compares against, so writing it without
    the facts would leave the parts never told, and writing the facts without
    it would tell them again on the next sync and every one after.
    """
    state = store.load_state(synced.work)
    store.publish_state(
        synced.work, state.changed(CORPUS_ORIGIN, changes(synced.arrivals))
    )
    store.publish_findings(synced.work, synced.found)
    return synced.dirtied()


def work_filter(tags: Iterable[str], *, since: str = "") -> CorpusFilter:
    """The filter one work's research runs under.

    Tags rather than a query, because tags are what the corpus is browsed by
    and what a vocabulary edit moves. ``since`` is there because a book that
    has been through one pass wants what has landed since it, and reading the
    whole corpus again to find out would be reading it again.
    """
    return CorpusFilter(tags=tuple(tags), since=since)
