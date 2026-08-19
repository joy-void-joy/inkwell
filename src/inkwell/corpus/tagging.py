"""Assigning a document's tags: derive what is declared, judge what is not.

Two halves, and the split between them is decided here rather than by whoever
calls in. Who published a document, which section it came from, and whatever
else its source declared are already facts the registry holds — re-deriving
them per document would spend a judgement to reproduce a declaration, and would
let the two disagree. So the registry's half is *derived*, and a judgement is
spent only on the one question a declaration cannot answer: what this
particular document is about.

That judgement reads the stored document, never the network. It is the same
pass that writes the document's summary, because a reader who has just read a
document to tag it can say what it is in a sentence for free — and because the
alternative is two sweeps over the corpus for one reading of each document.
Assignment reading the corpus rather than the web is also what makes a
vocabulary edit cheap: ``retag_source`` re-tags what is already stored without
a single fetch.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkwell.corpus.registry import (
    SourceDeclaration,
    corpus_vocabulary,
)
from inkwell.corpus.storage import (
    CorpusStore,
    SourceShard,
    StoredDocument,
    now_stamp,
)
from inkwell.corpus.tags import DocumentTags, TagTerm, TagVocabulary, folded

logger = logging.getLogger(__name__)

SUMMARY_WORDS = 40
"""How long the judged summary may run. It stands in for an abstract where the
source published none, so it has to be readable in a listing of two hundred."""

PDF_JUDGING_PAGES = 6
"""How much of a PDF a judgement reads before deciding. The front matter is
what says what a paper is about; reading all of a hundred-page appendix would
cost far more and settle nothing the abstract did not."""


class TagRequest(BaseModel):
    """One document put to a judgement, and the terms it may choose from.

    Carries the path rather than the text: a stored document may be a PDF, and
    a PDF is read at a page range rather than transcribed. Handing over the
    path keeps both kinds one request.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str
    url: str = ""
    source: str = ""
    category: str = ""
    kind: str = "markdown"
    path: str = ""
    page_count: int = 0
    abstract: str = ""
    choices: tuple[TagTerm, ...] = ()

    def reading_hint(self) -> str:
        """How to read this document, which differs for a PDF."""
        if self.kind != "pdf":
            return f"Read {self.path}"
        pages = min(self.page_count, PDF_JUDGING_PAGES) or PDF_JUDGING_PAGES
        return (
            f"Read {self.path} with pages='1-{pages}' — it is a "
            f"{self.page_count or 'unknown'}-page PDF, and its opening is what "
            "says what it is about"
        )

    def offered(self) -> str:
        """The vocabulary as the judgement reads it, one term per line."""
        return "\n".join(f"- {term.tag}: {term.description}" for term in self.choices)

    def prompt(self) -> str:
        """What the judgement is asked, for this document."""
        return "\n".join(
            [
                f"Document: {self.title or self.slug}",
                f"Published at: {self.url}" if self.url else "",
                f"Source: {self.source}" if self.source else "",
                f"Section: {self.category}" if self.category else "",
                f"Opening: {self.abstract}" if self.abstract else "",
                "",
                self.reading_hint(),
                "",
                "Declared tags, any number of which may apply:",
                self.offered(),
            ]
        )


class TagJudgement(BaseModel):
    """What a judgement made of one document.

    Tags and summary together because they come from one reading. Splitting
    them into two passes would double what tagging costs and halve what the
    second pass knows.
    """

    model_config = ConfigDict(frozen=True)

    tags: tuple[str, ...] = ()
    summary: str = ""


class TagJudge(ABC):
    """Who decides what a document is about.

    A seam, only ever injected: nothing here constructs one for itself, so a
    test hands over a judge that reads the fixture and a run hands over one
    that spends a model.
    """

    @abstractmethod
    async def judge(self, request: TagRequest) -> TagJudgement | None:
        """Tag and summarize one document, or return None if it could not."""


JUDGE_SYSTEM = """You tag documents for a research corpus that is browsed \
rather than searched: someone scanning titles and tags is how a document gets \
found at all, so a tag that is merely plausible costs more than a missing one.

Read the document, then:

- Choose every declared tag that genuinely applies. Judge what the document is \
about and how it argues, not which words appear in it. Most documents earn two \
to four tags; choosing none is the right answer for a document the vocabulary \
does not cover, and padding the list to look thorough makes every filter worse.
- Propose a tag of your own ONLY for a substantial subject the declared list \
misses entirely. These are reviewed and promoted into the vocabulary, so \
propose the subject a dozen documents would share, never one this document \
alone would carry.
- Write a summary of at most {words} words saying what the document claims or \
reports. It stands in for an abstract where the source published none, so it \
is what the document says, not what kind of thing it is."""


class ModelTagJudge(TagJudge):
    """A judgement spent per document, once, when it enters the corpus.

    Affordable exactly because of the scale this corpus is built for: hundreds
    to low thousands of documents, tagged once and browsed for free thereafter.

    ``model`` empty resolves the classify stage's at call time rather than at
    construction, so a judge built once still follows the settings a session
    later selects.
    """

    def __init__(self, model: str = "", summary_words: int = SUMMARY_WORDS) -> None:
        self.model = model
        self.summary_words = summary_words

    async def judge(self, request: TagRequest) -> TagJudgement | None:
        from inkwell.agent.client import query
        from inkwell.agent.config import stage_model

        class JudgedDocument(BaseModel):
            tags: list[str] = Field(
                description=(
                    "Every tag that applies — declared tags spelled exactly as "
                    "offered, plus any proposed subject the list misses"
                )
            )
            summary: str = Field(
                description=f"What the document says, in at most "
                f"{self.summary_words} words"
            )

        judged = await query(
            request.prompt(),
            output_type=JudgedDocument,
            model=self.model or stage_model("classify"),
            system_prompt=JUDGE_SYSTEM.format(words=self.summary_words),
            tools=["Read"],
            autonomy="unattended",
        )
        if judged is None:
            return None
        return TagJudgement(tags=tuple(judged.tags), summary=judged.summary)


class Assignment(BaseModel):
    """What one pass decided about a document, and why it decided no more.

    ``failure`` is what separates "read it and it is about nothing we track"
    from "could not read it": the first is a real gap a browse should show, the
    second is a document to try again.
    """

    model_config = ConfigDict(frozen=True)

    tags: DocumentTags
    summary: str = ""
    failure: str = ""

    def applied_to(self, document: StoredDocument) -> StoredDocument:
        """``document``, carrying what this pass decided.

        A pass that failed leaves the previous summary standing rather than
        blanking it: the earlier reading is still the best thing anyone knows
        about the document.
        """
        return document.model_copy(
            update={"tags": self.tags, "summary": self.summary or document.summary}
        )

    def changed(self, document: StoredDocument) -> bool:
        """Whether this would leave the document tagged differently."""
        return self.tags.applied() != document.tags.applied()


class DocumentTagger(BaseModel):
    """Assigns one document's tags, deriving what it can and judging the rest.

    Holds the vocabulary and the judge rather than being one: what is derived
    and what is judged is settled here, and the caller only chooses who does
    the judging.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vocabulary: TagVocabulary = Field(default_factory=corpus_vocabulary)
    judge: TagJudge = Field(default_factory=ModelTagJudge)

    async def judged(self, request: TagRequest) -> TagJudgement | None:
        """Put one document to the judge, surviving whatever it does.

        A judgement is a delegated session, so it fails in every way a session
        can. None of those is worth losing a stored document over — the
        document keeps its derived tags and a later re-tag tries again.
        """
        try:
            return await self.judge.judge(request)
        except Exception:
            logger.exception("Tagging %s could not be judged", request.slug)
            return None

    def request_for(
        self, declaration: SourceDeclaration, document: StoredDocument, path: Path
    ) -> TagRequest:
        """What the judge is shown: this document, and the terms it may pick."""
        return TagRequest(
            slug=document.slug,
            title=document.title,
            url=document.url,
            source=declaration.display_name,
            category=document.category,
            kind=document.kind,
            path=str(path),
            page_count=document.page_count,
            abstract=document.abstract,
            choices=self.vocabulary.judged_terms(),
        )

    def unjudged(
        self, document: StoredDocument, derived: tuple[str, ...], failure: str
    ) -> Assignment:
        """What a document carries when the judgement did not come back.

        The derived half is refreshed regardless — it costs nothing and the
        declaration is current — and whatever an earlier pass judged stands, so
        a failure today never un-tags a document that was tagged yesterday.
        """
        logger.warning("Tagging %s: %s", document.slug, failure)
        return Assignment(
            tags=document.tags.model_copy(update={"derived": derived}),
            summary=document.summary,
            failure=failure,
        )

    async def assign(
        self, declaration: SourceDeclaration, document: StoredDocument, path: Path
    ) -> Assignment:
        """Tag one stored document: derived from its source, judged from itself."""
        derived = declaration.tags_for(document.category)
        if not path.is_file():
            return self.unjudged(document, derived, f"no stored body at {path}")

        judgement = await self.judged(self.request_for(declaration, document, path))
        if judgement is None:
            return self.unjudged(document, derived, "the judgement did not come back")

        matched = [
            (proposal, self.vocabulary.resolve(proposal)) for proposal in judgement.tags
        ]
        core = sorted({term.tag for _, term in matched if term is not None})
        proposed = {folded(proposal) for proposal, term in matched if term is None} - {
            *core,
            *derived,
            "",
        }
        return Assignment(
            tags=DocumentTags(
                derived=derived,
                core=tuple(core),
                free=tuple(sorted(proposed)),
                judged=True,
                vocabulary=self.vocabulary.signature(),
                assigned_at=now_stamp(),
            ),
            summary=judgement.summary,
        )


class RetagReport(BaseModel):
    """What re-tagging one source did, in the terms a vocabulary edit asks in.

    A vocabulary change is only worth making if its effect is visible, so the
    counts here are the effect: how many documents now carry different tags,
    and how many the new vocabulary has nothing to say about.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    examined: int = 0
    retagged: int = 0
    current: int = 0
    changed: int = 0
    untagged: int = 0
    failed: int = 0

    def summary(self) -> str:
        """One line naming what happened, for a log or a CLI."""
        counted = " | ".join(
            f"{name} {value}"
            for name, value in (
                ("examined", self.examined),
                ("retagged", self.retagged),
                ("changed", self.changed),
                ("untagged", self.untagged),
                ("up to date", self.current),
                ("failed", self.failed),
            )
        )
        return f"{self.source}: {counted}"


class RetagStep(BaseModel):
    """One document judged, told to whoever is watching the run.

    Reported *from* the run rather than read off the corpus, which is where
    this differs from a sweep's progress. A sweep adds documents, so counting
    what is on disk tells you how far it got; judging rewrites tags in place
    and leaves the count alone, so a watcher reading the store sees the same
    number throughout and cannot tell a run that has judged nothing from one
    that has judged half.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    done: int
    total: int


def stale_documents(
    shard: SourceShard, vocabulary: TagVocabulary, *, force: bool = False
) -> tuple[StoredDocument, ...]:
    """The documents a re-tag would judge — those not current against ``vocabulary``.

    Separate from ``retag_source`` so a caller can size the work before
    starting it: a bar over documents needs its denominator before the first
    judgement, and this is a read of the index rather than a second pass over
    the corpus.
    """
    return tuple(
        document
        for document in shard.documents
        if force or not document.tags.current(vocabulary)
    )


DEFAULT_RETAG_CONCURRENCY = 4
"""How many documents of one source are judged at once. Each is a delegated
session, so this is a ceiling on sessions rather than on requests."""


async def retag_source(
    declaration: SourceDeclaration,
    store: CorpusStore,
    tagger: DocumentTagger,
    *,
    force: bool = False,
    concurrency: int = DEFAULT_RETAG_CONCURRENCY,
    progress: Callable[[RetagStep], None] | None = None,
) -> RetagReport:
    """Re-tag one source's stored documents, fetching nothing.

    Reads the index and the bodies already on disk, which is what makes a
    vocabulary edit cost model time rather than a re-crawl. Documents already
    judged against this exact vocabulary are left alone unless ``force`` says
    otherwise, so re-running after no edit is nearly free — and costs no write
    either, which keeps a source nothing was stored for from gaining an index
    that says it holds nothing.

    Each judgement is recorded as it lands rather than at the end, so a run
    interrupted partway through a source keeps every judgement it paid for.
    Writing the index costs nothing against the model call that preceded it,
    and recording is synchronous, so the concurrent judgements never interleave
    with each other's writes.
    """
    shard = store.load(declaration)
    limiter = asyncio.Semaphore(concurrency)
    stale = stale_documents(shard, tagger.vocabulary, force=force)
    judged = 0

    async def reassign(document: StoredDocument) -> Assignment:
        nonlocal judged
        async with limiter:
            assignment = await tagger.assign(
                declaration, document, store.document_path(declaration.key, document)
            )
        shard.record_document(assignment.applied_to(document))
        store.save(shard)
        judged += 1
        if progress is not None:
            progress(RetagStep(source=declaration.key, done=judged, total=len(stale)))
        return assignment

    assignments = list(await asyncio.gather(*(reassign(one) for one in stale)))

    return RetagReport(
        source=declaration.key,
        examined=len(shard.documents),
        retagged=len(stale),
        current=len(shard.documents) - len(stale),
        changed=sum(
            1
            for document, assignment in zip(stale, assignments)
            if assignment.changed(document)
        ),
        untagged=sum(1 for one in assignments if one.tags.untagged()),
        failed=sum(1 for one in assignments if one.failure),
    )


async def retag_corpus(
    declarations: tuple[SourceDeclaration, ...],
    store: CorpusStore,
    tagger: DocumentTagger | None = None,
    *,
    force: bool = False,
    concurrency: int = DEFAULT_RETAG_CONCURRENCY,
    progress: Callable[[RetagStep], None] | None = None,
) -> tuple[RetagReport, ...]:
    """Re-tag several sources in turn, reporting each.

    The per-source report comes when that source finishes, so a run over a
    dozen sources says nothing for as long as it takes unless something is
    watching ``progress`` — which is why the callback is per document rather
    than per source.
    """
    assigning = tagger if tagger is not None else DocumentTagger()
    reports = [
        await retag_source(
            declaration,
            store,
            assigning,
            force=force,
            concurrency=concurrency,
            progress=progress,
        )
        for declaration in declarations
    ]
    logger.info(
        "Re-tagging finished\n%s", "\n".join(report.summary() for report in reports)
    )
    return tuple(reports)
