"""Corpus tools — read what is already gathered, and top up what is thin.

Every other research tool here is a search box: a query goes out, and whatever
it happened to name comes back. These are the other kind. The corpus already
holds what its sources published, so the questions worth asking of it are
*what is in here* and *is that enough* — and the answer to the second one is
sometimes "fetch more of this source before continuing".

The corpus is files, so the reading half is deliberately thin: these tools give
the paths, the per-source counts, and the title-and-category listing an agent
judges coverage from, and then Grep and Read do the actual work. Adding a query
layer here would put a worse search engine in front of tools that already do it
better.
"""

import logging

from pydantic import BaseModel, Field

from inkwell.agent.config import corpus_root
from inkwell.corpus.fetch import DocumentFetcher, corpus_client
from inkwell.corpus.ingest import SourceReport, ingest_source
from inkwell.corpus.registry import (
    DECLARED_SOURCES,
    SourceDeclaration,
    declaration_for,
)
from inkwell.corpus.storage import CorpusStore, SourceShard
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

MAX_TITLES = 200
"""How many titles one listing returns. A source holding more than this is one
an agent should Grep rather than read a catalogue of."""

CORPUS_LAYOUT = (
    "<root>/<source>/index.json holds the typed index; <root>/<source>/<slug>.md "
    "holds a document extracted to Markdown; <root>/<source>/<slug>.pdf holds one "
    "that is a PDF. PDFs are never text-extracted — Read them directly with a "
    "pages='N-M' range, using the index's page_count to choose it."
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
    authority: str = Field(
        description=(
            "What kind of standing its word has: 'first-party' (the "
            "organization describing its own work), 'government', 'institute', "
            "or 'independent'"
        )
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
    failures: int = Field(
        default=0, description="Documents that failed to fetch and will be retried"
    )
    updated_at: str = Field(default="", description="When ingestion last ran for it")
    directory: str = Field(default="", description="Where its files are, for Grep/Read")
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
        failures=len(shard.failures),
        updated_at=shard.updated_at,
        directory=directory,
        note=declaration.notes,
    )


class CorpusDocumentEntry(BaseModel):
    """One stored document, as a coverage judgement needs to see it."""

    slug: str = Field(description="Identifier within the source")
    title: str = Field(description="Document title")
    category: str = Field(description="Which section of the source it came from")
    kind: str = Field(description="'markdown' or 'pdf'")
    path: str = Field(description="File to Read")
    url: str = Field(description="Where it was published")
    words: int = Field(default=0, description="Word count (0 for a PDF)")
    page_count: int = Field(default=0, description="Pages, for a PDF")
    quality_score: float = Field(default=1.0, description="Heuristic score, 0-1")
    quality_flags: tuple[str, ...] = Field(
        default=(), description="Rule ids that fired, which the score derives from"
    )
    quality_assessed: bool = Field(
        default=True,
        description="False for a PDF, whose text is never extracted to be judged",
    )
    abstract: str = Field(default="", description="Opening prose, for relevance")


def document_entries(
    shard: SourceShard,
    directory: str,
    *,
    category: str = "",
    since: tuple[str, ...] = (),
) -> list[CorpusDocumentEntry]:
    """The shard's stored documents, optionally one category or one added set."""
    return [
        CorpusDocumentEntry(
            slug=document.slug,
            title=document.title,
            category=document.category,
            kind=document.kind,
            path=f"{directory}/{document.filename}",
            url=document.url,
            words=document.words,
            page_count=document.page_count,
            quality_score=document.quality.score,
            quality_flags=document.quality.fired,
            quality_assessed=document.quality.assessed,
            abstract=document.abstract,
        )
        for document in shard.documents
        if not category or document.category == category
        if not since or document.slug in since
    ]


class CorpusOverviewInput(BaseModel):
    pass


class CorpusOverviewOutput(BaseModel):
    root: str = Field(description="Corpus root — Grep and Read work under this path")
    sources: list[CorpusSourceSummary] = Field(
        description="Every declared source and what the corpus holds for it"
    )
    layout: str = Field(description="How the files under the root are arranged")


class CorpusTitlesInput(BaseModel):
    source: str = Field(description="Which source's documents to list")
    category: str = Field(
        default="", description="Restrict to one category (default: all)"
    )


class CorpusTitlesOutput(BaseModel):
    source: str = Field(description="Source listed")
    directory: str = Field(description="Where these files are")
    documents: list[CorpusDocumentEntry] = Field(description="Stored documents")
    truncated: bool = Field(
        default=False, description="True when more documents were held than returned"
    )
    listed_but_absent: int = Field(
        default=0,
        description=(
            "Documents the source publishes that are not stored yet — top up if "
            "this matters for the question at hand"
        ),
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
    added: list[CorpusDocumentEntry] = Field(
        default_factory=list, description="What this call actually added"
    )
    report: str = Field(description="One-line summary of the run")


@lup_tool(
    "List the research corpus: every tracked source, how many documents are "
    "held for it, which categories they fall under, and where its files live. "
    "Use this FIRST when a question is about AI labs, safety institutes, or AI "
    "policy — the corpus already holds what these sources published, so reading "
    "it beats searching for it. It also names each source's venue and authority, "
    "so a citation can state standing without asserting it. Then Grep the "
    "directory for wording and Read the files, or call corpus_titles to judge "
    "whether coverage is good enough.",
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
        layout=CORPUS_LAYOUT,
    )


@lup_tool(
    "List one corpus source's stored documents — title, category, path, "
    "abstract, and quality — plus how many documents the source publishes that "
    "are NOT stored yet. Use this to judge whether the corpus covers a question "
    "before relying on it: titles and categories are what tell you a source has "
    "the material, and listed_but_absent is what tells you it is thin. Read the "
    "paths directly; for a PDF, Read with pages='N-M' using page_count.",
    name="corpus_titles",
)
async def corpus_titles(inp: CorpusTitlesInput) -> CorpusTitlesOutput:
    corpus = store()
    declaration = declared(inp.source)
    shard = corpus.load_by_name(inp.source)
    if shard is None:
        raise ToolError(
            f"Nothing ingested for {inp.source!r} yet. Use top_up_corpus to gather "
            f"some, or corpus_overview to see which sources are held."
        )
    directory = str(corpus.source_dir(declaration.key))
    held = document_entries(shard, directory, category=inp.category)
    stored_slugs = {document.slug for document in shard.documents}
    return CorpusTitlesOutput(
        source=inp.source,
        directory=directory,
        documents=held[:MAX_TITLES],
        truncated=len(held) > MAX_TITLES,
        listed_but_absent=sum(
            1
            for entry in shard.discovered
            if entry.present and entry.slug not in stored_slugs
        ),
    )


@lup_tool(
    "Fetch more of one corpus source, for when its coverage is too thin for the "
    "question. Enumerates what the source publishes and stores what is missing, "
    "up to a limit, then reports exactly what it added — so the research notes "
    "can say how the corpus changed. Documents already held are not refetched. "
    "Use this after corpus_titles shows listed_but_absent is high and the "
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

    after = corpus.load(declaration)
    directory = str(corpus.source_dir(declaration.key))
    fresh = tuple(
        document.slug
        for document in after.documents
        if document.slug not in held_before
    )
    return TopUpCorpusOutput(
        source=inp.source,
        discovered=report.discovered,
        stored=report.stored,
        unchanged=report.unchanged,
        skipped=report.skipped,
        failed=report.failed,
        added=document_entries(after, directory, since=fresh),
        report=report.summary(),
    )


CORPUS_TOOLS = [corpus_overview, corpus_titles, top_up_corpus]
