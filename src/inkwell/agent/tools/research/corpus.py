"""Corpus tools — read what is already gathered, and top up what is thin.

Every other research tool here is a search box: a query goes out, and whatever
it happened to name comes back. These are the other kind. The corpus already
holds what its sources published, so the questions worth asking of it are
*what is in here*, *what does it say*, and *is that enough* — and the answer to
the last one is sometimes "fetch more of this source before continuing".

Three tools, and the split between them is by what is being asked rather than
by how it is answered. ``corpus_overview`` says what sources exist and what
vocabulary a browse narrows on. ``corpus_search`` is the whole of retrieval:
one tool with the modes named in its description, so an agent picks by what it
is asking rather than by guessing among near-identical tools. ``top_up_corpus``
is the only one that writes.

Retrieval itself lives in ``corpus.retrieval``, which navigates the typed index
structurally — the fields the shard declares, read through the models that
declare them. These tools are the surface: they take a question, hand it to
that engine, and return what it found.
"""

import logging

from pydantic import BaseModel, Field

from inkwell.agent.config import corpus_root, corpus_semantics
from inkwell.agent.provenance import Venue
from inkwell.corpus.fetch import DocumentFetcher, corpus_client
from inkwell.corpus.ingest import SourceReport, ingest_source
from inkwell.corpus.registry import (
    DECLARED_SOURCES,
    SourceDeclaration,
    corpus_vocabulary,
    declaration_for,
)
from inkwell.corpus.retrieval import (
    ORDER_KEYS,
    TIERS,
    CorpusAnswer,
    CorpusFilter,
    CorpusHit,
    CorpusQuery,
    OrderName,
    Ordering,
    TierName,
    hit_for,
    read_index,
    retrieval_tier,
    search_corpus,
)
from inkwell.corpus.storage import CorpusStore, DocumentKind, SourceShard
from inkwell.corpus.tags import TagTerm
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

CORPUS_LAYOUT = (
    "<root>/<source>/index.json holds the typed index; <root>/<source>/<slug>.md "
    "holds a document extracted to Markdown; <root>/<source>/<slug>.pdf holds one "
    "that is a PDF. corpus_search navigates that index for you — reach for it "
    "before reading the files yourself. PDFs are never text-extracted: Read them "
    "directly with the pages range corpus_search hands back."
)


def store() -> CorpusStore:
    return CorpusStore(root=corpus_root())


def declared(key: str) -> SourceDeclaration:
    """The declaration under ``key``, or a tool error naming what is declared."""
    declaration = declaration_for(key)
    if declaration is None:
        known = ", ".join(entry.key for entry in DECLARED_SOURCES)
        raise ToolError(f"No such corpus source: {key!r}. Declared: {known}")
    return declaration


class CorpusSourceSummary(BaseModel):
    """One source's standing in the corpus, and what it is worth citing as."""

    key: str = Field(description="Source key; also its directory under the corpus root")
    display_name: str = Field(description="How to name the source to a reader")
    organization: str = Field(description="Who publishes it")
    venue: str = Field(description="The venue a citation of it should name")
    authority: Venue = Field(
        description="What kind of standing its word has, in the venue vocabulary"
    )
    active: bool = Field(description="Whether ingestion sweeps this source at all")
    stored: int = Field(default=0, description="Documents held for this source")
    listed: int = Field(
        default=0, description="Documents the source currently lists as published"
    )
    pdfs: int = Field(default=0, description="How many stored documents are PDFs")
    categories: tuple[str, ...] = Field(
        default=(), description="The sections its stored documents fall under"
    )
    tags: tuple[str, ...] = Field(
        default=(),
        description="Tags its stored documents carry — what a browse of it can narrow on",
    )
    untagged: int = Field(
        default=0,
        description=(
            "Documents a tagging pass read and found nothing to say about, so "
            "no subject browse reaches them"
        ),
    )
    failures: int = Field(
        default=0, description="Documents that failed to fetch and will be retried"
    )
    updated_at: str = Field(default="", description="When ingestion last ran for it")
    directory: str = Field(default="", description="Where its files are")
    note: str = Field(
        default="",
        description="What to know before trusting the above — why a source is inactive",
    )

    def coverage_gap(self) -> int:
        """How many listed documents are not stored yet."""
        return max(0, self.listed - self.stored)


def summarize(
    declaration: SourceDeclaration, shard: SourceShard, directory: str
) -> CorpusSourceSummary:
    """One source's summary. A source nothing was ingested for still appears."""
    return CorpusSourceSummary(
        key=declaration.key,
        display_name=declaration.display_name,
        organization=declaration.organization,
        venue=declaration.venue,
        authority=declaration.authority,
        active=declaration.active,
        stored=len(shard.documents),
        listed=sum(1 for entry in shard.discovered if entry.present),
        pdfs=sum(1 for document in shard.documents if document.is_pdf()),
        categories=shard.categories(),
        tags=shard.tags(),
        untagged=len(shard.untagged()),
        failures=len(shard.failures),
        updated_at=shard.updated_at,
        directory=directory,
        note=declaration.notes,
    )


class RetrievalMode(BaseModel):
    """One way of asking the corpus a question, and which question it answers.

    Declared rather than described in prose so that the tool's own output can
    list the modes back: an agent that reached for the wrong one reads what the
    others are in the same result, instead of having to remember a description
    it saw once.
    """

    name: str = Field(description="How this mode is asked for")
    answers: str = Field(description="The kind of question it is the answer to")


RETRIEVAL_MODES: tuple[RetrievalMode, ...] = (
    RetrievalMode(
        name="filter",
        answers=(
            "what the corpus holds on a subject, from a publisher, in a period "
            "— pass tags, sources, organizations, venues, authorities, "
            "categories, since/until, kinds, or title_contains. This is the "
            "primary mode: the index declares these fields and answers them "
            "without opening a single document"
        ),
    ),
    RetrievalMode(
        name="phrase",
        answers=(
            "where a particular wording appears — the full-text fallback, for a "
            "turn of phrase, a figure, or a name no tag covers. Reads the stored "
            "Markdown bodies, so it is slower and blind to PDFs, and it is never "
            "the way to answer a question about a tag, a date, or a venue"
        ),
    ),
    RetrievalMode(
        name="like",
        answers=(
            "what is near a question that shares no vocabulary with the corpus "
            "— the optional semantic layer. It reports itself absent when it is "
            "switched off or nothing is embedded, and the structural result "
            "comes back regardless"
        ),
    ),
)
"""The three ways of asking, each named by the question it answers."""


class CorpusOverviewInput(BaseModel):
    pass


class CorpusOverviewOutput(BaseModel):
    root: str = Field(description="Corpus root — where the sharded indexes live")
    sources: list[CorpusSourceSummary] = Field(
        description="Every declared source and what the corpus holds for it"
    )
    vocabulary: list[TagTerm] = Field(
        default_factory=list,
        description=(
            "Every tag a browse can filter on, and what each covers. This is "
            "the declared set — pass one to corpus_search's tags to narrow by it"
        ),
    )
    modes: list[RetrievalMode] = Field(
        default_factory=list,
        description="How corpus_search can be asked, and what each mode answers",
    )
    tiers: list[str] = Field(
        default_factory=list,
        description="The depths corpus_search answers at, and what each shows",
    )
    orderings: list[str] = Field(
        default_factory=list,
        description=(
            "The axes corpus_search's order_by accepts. It defaults to recency "
            "then quality — most recent first, better capture breaking a tie"
        ),
    )
    layout: str = Field(description="How the files under the root are arranged")


class CorpusSearchInput(BaseModel):
    """One question put to the corpus. Every field is optional; none narrows by
    default, so an empty call is a browse of everything held."""

    sources: list[str] = Field(
        default_factory=list,
        description="Source keys from corpus_overview (default: every source held)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Tags a document must carry, all of them, spelled as "
            "corpus_overview's vocabulary spells them"
        ),
    )
    organizations: list[str] = Field(
        default_factory=list,
        description="Publishing organizations, as the registry names them",
    )
    venues: list[str] = Field(
        default_factory=list, description="Publication venues a citation would name"
    )
    authorities: list[Venue] = Field(
        default_factory=list,
        description=(
            "Standing, in the venue vocabulary — lab_publication, "
            "official_report, peer_reviewed, and the rest"
        ),
    )
    categories: list[str] = Field(
        default_factory=list, description="Sections of a source, from corpus_overview"
    )
    title_contains: list[str] = Field(
        default_factory=list, description="Words a title must contain, all of them"
    )
    since: str = Field(default="", description="On or after this date, YYYY-MM-DD")
    until: str = Field(default="", description="On or before this date, YYYY-MM-DD")
    kinds: list[DocumentKind] = Field(
        default_factory=list, description="'markdown' or 'pdf' (default: both)"
    )
    min_quality: float = Field(
        default=0.0, description="Lowest recorded quality score to keep, 0-1"
    )
    untagged_only: bool = Field(
        default=False,
        description=(
            "Only documents a tagging pass read and found nothing to say about "
            "— the corpus's blind spot, worth looking at when a browse comes up "
            "empty on a subject you expected"
        ),
    )
    phrase: str = Field(
        default="",
        description=(
            "Full-text fallback: hunt this literal phrase through the stored "
            "Markdown bodies. Use it for a wording no field answers, never for "
            "a tag, a date, a venue, or a source"
        ),
    )
    like: str = Field(
        default="",
        description=(
            "Semantic mode: order results by nearness to this question. The "
            "layer is optional — when it is off or nothing is embedded, the "
            "result says so and comes back ordered structurally instead"
        ),
    )
    tier: TierName = Field(
        default="browse",
        description=(
            "How much of each document to show. 'browse' is titles, tags, and "
            "locators only, and is cheap; 'narrow' adds each abstract or "
            "summary; 'read' hands over the path and the PDF page ranges"
        ),
    )
    order_by: list[OrderName] = Field(
        default_factory=list,
        description=(
            "Override the ordering. Default is recency then quality — most "
            "recent first, better capture breaking a tie"
        ),
    )
    oldest_first: bool = Field(
        default=False,
        description="Turn the ordering around, so the first result is the last one",
    )
    limit: int = Field(
        default=0, description="How many documents to return (default: the tier's own)"
    )


class TopUpCorpusInput(BaseModel):
    source: str = Field(
        description="Which source to fetch more of, by its key from corpus_overview"
    )
    limit: int = Field(
        default=25,
        description=(
            "How many not-yet-stored documents to fetch. Keep it modest: this "
            "runs during the research stage and each document is a live fetch."
        ),
    )


class TopUpCorpusOutput(BaseModel):
    source: str = Field(description="Source topped up")
    discovered: int = Field(description="Documents the source currently lists")
    stored: int = Field(description="Documents newly written to the corpus")
    unchanged: int = Field(description="Documents refetched and found identical")
    skipped: int = Field(description="Documents left for a later run by the limit")
    failed: int = Field(description="Documents that could not be fetched")
    added: list[CorpusHit] = Field(
        default_factory=list, description="What this call actually added"
    )
    report: str = Field(description="One-line summary of the run")


@lup_tool(
    "List the research corpus: every tracked source, how many documents are "
    "held for it, which categories and tags they fall under, the tag "
    "vocabulary a browse narrows on, and the modes corpus_search answers in. "
    "Use this FIRST when a question is about AI labs, safety institutes, or AI "
    "policy — the corpus already holds what these sources published, so "
    "reading it beats searching the web for it, and this call is what tells "
    "you whether it does. It also names each source's venue and authority, so "
    "a citation can state standing without asserting it. Then call "
    "corpus_search with a tag, a date range, or a source to narrow.",
    name="corpus_overview",
)
async def corpus_overview(_inp: CorpusOverviewInput) -> CorpusOverviewOutput:
    corpus = store()
    return CorpusOverviewOutput(
        root=str(corpus.root),
        sources=[
            summarize(
                declaration,
                corpus.load(declaration),
                str(corpus.source_dir(declaration.key)),
            )
            for declaration in DECLARED_SOURCES
        ],
        vocabulary=list(corpus_vocabulary().terms()),
        modes=list(RETRIEVAL_MODES),
        tiers=[f"{tier.name}: {tier.answers}" for tier in TIERS],
        orderings=[f"{key.name}: {key.description}" for key in ORDER_KEYS],
        layout=CORPUS_LAYOUT,
    )


@lup_tool(
    "Search the research corpus by navigating its index. Reach for this "
    "BEFORE the external search boxes whenever a question is about what the "
    "tracked sources published — AI labs, safety institutes, AI policy: the "
    "material is already gathered, so this reads it rather than searching for "
    "it again, and it comes back with the venue and standing a citation needs. "
    "It answers four kinds of question, so pick by what you are asking:\n"
    "  • BROWSE BY TAG — pass tags from corpus_overview's vocabulary to narrow "
    "hundreds of documents to the ones on your subject.\n"
    "  • FILTER BY FIELD — sources, organizations, venues, authorities, "
    "categories, since/until dates, kinds, title_contains. These are declared "
    "index fields, answered without opening any document.\n"
    "  • HUNT A PHRASE — 'phrase' is the full-text fallback over the stored "
    "Markdown bodies, for a wording no field covers. Slower, blind to PDFs, "
    "and never the way to answer a tag, date, venue, or source question.\n"
    "  • FIND A SEMANTIC NEIGHBOUR — 'like' orders by nearness to a question "
    "that shares no vocabulary with the corpus. Optional: it says so when it "
    "is off, and the structural result comes back either way.\n"
    "Three depths, independently callable: tier='browse' is titles, tags, and "
    "locators (cheap, start here), 'narrow' adds each abstract or judged "
    "summary, 'read' hands over the path and PDF page ranges for Read. Results "
    "come back most recent first with quality breaking ties, which order_by "
    "and oldest_first override. Every result carries its locator, so you can "
    "Read straight from a browse.",
    name="corpus_search",
)
async def corpus_search(inp: CorpusSearchInput) -> CorpusAnswer:
    if inp.phrase and inp.like:
        raise ToolError(
            "Pass either 'phrase' (hunt this exact wording in the bodies) or "
            "'like' (order by nearness to this question), not both — they "
            "answer different questions, and running them together would "
            "leave you unable to tell which one put a document in the result."
        )

    corpus = store()
    query = CorpusQuery(
        where=CorpusFilter(
            sources=tuple(inp.sources),
            organizations=tuple(inp.organizations),
            venues=tuple(inp.venues),
            authorities=tuple(inp.authorities),
            categories=tuple(inp.categories),
            tags=tuple(inp.tags),
            title_contains=tuple(inp.title_contains),
            since=inp.since,
            until=inp.until,
            kinds=tuple(inp.kinds),
            min_quality=inp.min_quality,
            untagged_only=inp.untagged_only,
        ),
        tier=inp.tier,
        ordering=Ordering(
            keys=tuple(inp.order_by) or Ordering().keys,
            descending=not inp.oldest_first,
        ),
        phrase=inp.phrase,
        like=inp.like,
        limit=inp.limit,
    )
    return await search_corpus(query, corpus, semantics=corpus_semantics(corpus))


@lup_tool(
    "Fetch more of one corpus source, for when its coverage is too thin for the "
    "question. Enumerates what the source publishes and stores what is missing, "
    "up to a limit, then reports exactly what it added — so the research notes "
    "can say how the corpus changed. Documents already held are not refetched. "
    "Use this after corpus_search shows listed_but_absent is high and the "
    "missing material matters; it makes live requests, so prefer a small limit.",
    name="top_up_corpus",
)
async def top_up_corpus(inp: TopUpCorpusInput) -> TopUpCorpusOutput:
    declaration = declared(inp.source)
    if not declaration.avenues:
        raise ToolError(
            f"{inp.source!r} is declared but cannot be enumerated yet "
            f"({declaration.notes or 'no avenues declared'}). Use the search and "
            "fetch tools for this source instead."
        )

    corpus = store()
    held_before = {document.slug for document in corpus.load(declaration).documents}

    async with corpus_client() as client:
        report: SourceReport = await ingest_source(
            declaration, corpus, DocumentFetcher(client=client), limit=inp.limit
        )
    if report.aborted:
        raise ToolError(f"Topping up {inp.source} failed: {report.aborted}")

    tier = retrieval_tier("narrow")
    return TopUpCorpusOutput(
        source=inp.source,
        discovered=report.discovered,
        stored=report.stored,
        unchanged=report.unchanged,
        skipped=report.skipped,
        failed=report.failed,
        added=[
            hit_for(entry, tier)
            for entry in read_index(corpus, (declaration.key,)).entries
            if entry.document.slug not in held_before
        ],
        report=report.summary(),
    )


CORPUS_TOOLS = [corpus_overview, corpus_search, top_up_corpus]
