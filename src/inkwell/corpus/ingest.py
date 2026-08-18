"""One ingestion run: enumerate, fetch what is new, store it, and report.

The report is the point as much as the corpus is. A sync that says only "done"
leaves an operator unable to tell a source that published nothing from one whose
sitemap moved, so every run returns counts per source — discovered, new,
stored, unchanged, skipped, failed — plus the failures themselves.

Nothing here aborts a run. A source that cannot be reached at all is recorded
and the remaining sources still sync, because the alternative is that one dead
host costs a night's ingestion. Failures land in the source's index, so the next
run retries them without anyone re-deriving what to retry.
"""

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from inkwell.corpus.discovery import AvenueFailure, PageCache, discover_avenues
from inkwell.pdf import page_count
from inkwell.corpus.failures import classify
from inkwell.corpus.fetch import (
    DocumentFetcher,
    FetchedDocument,
    HttpPageReader,
    corpus_client,
)
from inkwell.corpus.quality import QualityReport, assess
from inkwell.corpus.registry import (
    DECLARED_SOURCES,
    SourceDeclaration,
    active_declarations,
    declaration_for,
)
from inkwell.corpus.storage import (
    CorpusStore,
    DiscoveredEntry,
    SourceShard,
    StoredDocument,
    StoreFailure,
    content_digest,
    merged_discovery,
    now_stamp,
    pending_entries,
)
from inkwell.corpus.tagging import DocumentTagger

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
"""How many documents of one source are fetched at once. Deliberately modest:
the corpus is built once and topped up, and a polite crawl outlasts a fast one."""

ABSTRACT_WORDS = 120
"""How much of a document's opening the index keeps as its abstract. Enough to
judge relevance from the index alone, short enough that the index stays small."""


def abstract_of(document: FetchedDocument) -> str:
    """A short abstract for the index — the opening prose, never a summary.

    A PDF contributes nothing here, and that is deliberate: taking an abstract
    out of a PDF would mean extracting its text, which is the thing this corpus
    does not do. What locates a PDF instead is its title, URL, and page count.
    """
    if document.kind == "pdf":
        return ""
    prose = [
        line
        for line in document.text.splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("|")
    ]
    words = " ".join(prose).split()
    # lup: ignore[silent-truncation] — the index's standing-in abstract, marked
    # with an ellipsis; the document itself is stored whole beside it
    opening = " ".join(words[:ABSTRACT_WORDS])
    return f"{opening}…" if len(words) > ABSTRACT_WORDS else opening


type DocumentStatus = Literal["stored", "unchanged", "failed"]

STORED: DocumentStatus = "stored"
UNCHANGED: DocumentStatus = "unchanged"
FAILED: DocumentStatus = "failed"


class DocumentOutcome(BaseModel):
    """What became of one document this run touched."""

    model_config = ConfigDict(frozen=True)

    slug: str
    url: str
    status: DocumentStatus
    kind: str = ""
    failure_class: str = ""
    error: str = ""


class SourceReport(BaseModel):
    """What one source's ingestion did, in the terms an operator asks in."""

    model_config = ConfigDict(frozen=True)

    source: str
    display_name: str = ""
    active: bool = True
    discovered: int = 0
    new: int = 0
    stored: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    crawl_degraded: bool = False
    crawl_suspect: bool = False
    avenue_failures: tuple[AvenueFailure, ...] = ()
    failures: tuple[DocumentOutcome, ...] = ()
    aborted: str = ""

    def healthy(self) -> bool:
        """Whether this source needs no attention before the next run."""
        return not self.aborted and not self.failed and not self.crawl_degraded

    def summary(self) -> str:
        """One line naming what happened, for a log or a CLI."""
        if self.aborted:
            return f"{self.source}: aborted — {self.aborted}"
        counted = " | ".join(
            f"{name} {value}"
            for name, value in (
                ("discovered", self.discovered),
                ("new", self.new),
                ("stored", self.stored),
                ("unchanged", self.unchanged),
                ("skipped", self.skipped),
                ("failed", self.failed),
            )
        )
        flags = [
            name
            for name, raised in (
                ("DEGRADED", self.crawl_degraded),
                ("partial", self.crawl_suspect),
                ("avenues failed", bool(self.avenue_failures)),
            )
            if raised
        ]
        tail = f" [{', '.join(flags)}]" if flags else ""
        return f"{self.source}: {counted}{tail}"


class IngestReport(BaseModel):
    """What a whole run did, source by source."""

    model_config = ConfigDict(frozen=True)

    sources: tuple[SourceReport, ...] = ()
    started_at: str = ""
    finished_at: str = ""

    def stored(self) -> int:
        return sum(report.stored for report in self.sources)

    def failed(self) -> int:
        return sum(report.failed for report in self.sources)

    def aborted_sources(self) -> tuple[str, ...]:
        return tuple(report.source for report in self.sources if report.aborted)

    def summary(self) -> str:
        """Every source's line, plus a total."""
        aborted = self.aborted_sources()
        total = f"total: stored {self.stored()} | failed {self.failed()}"
        if aborted:
            total += f" | aborted {', '.join(aborted)}"
        return "\n".join([*(report.summary() for report in self.sources), total])


class StoredPayload(BaseModel):
    """A document written to disk, and what the index should say about it."""

    model_config = ConfigDict(frozen=True)

    filename: str
    page_count: int = 0
    quality: QualityReport = QualityReport()


class SourceIngestor(BaseModel):
    """Ingests one source: what to fetch, where to put it, what to report."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    declaration: SourceDeclaration
    store: CorpusStore
    fetcher: DocumentFetcher
    tagger: DocumentTagger = Field(default_factory=DocumentTagger)
    concurrency: int = DEFAULT_CONCURRENCY
    limit: int = 0
    refresh: bool = False

    def write(self, slug: str, document: FetchedDocument, title: str) -> StoredPayload:
        """Write one document in the form it arrived in, and judge it if it is text.

        A PDF is written byte for byte and left unassessed: the rules read text,
        and there is no text here that was not extracted from the PDF — which is
        what the corpus refuses to do. Its page count stands in as the thing a
        reader needs to open it at the right place.
        """
        source = self.declaration.key
        if document.kind == "pdf":
            filename = self.store.store_pdf(source, slug, document.data)
            return StoredPayload(
                filename=filename,
                page_count=page_count(self.store.source_dir(source) / filename),
                quality=QualityReport(assessed=False),
            )
        return StoredPayload(
            filename=self.store.store_markdown(source, slug, document.text),
            quality=assess(
                document.text, title=title, rules=self.declaration.quality_rules
            ),
        )

    async def store_one(
        self, entry: DiscoveredEntry, shard: SourceShard
    ) -> DocumentOutcome:
        """Fetch and store one document, or record why it could not be.

        An unchanged document is recognised by its content hash rather than by
        any timestamp, so re-running over a static source rewrites nothing.

        Tagging happens here, as the document enters, rather than when some
        later run happens to want it: tags are what a browse navigates by, so a
        corpus tagged only where someone already looked is one whose gaps are
        exactly where nobody has looked yet.
        """
        source = self.declaration.key
        try:
            fetched = await self.fetcher.fetch(entry.url, self.declaration)
            digest = content_digest(fetched.payload())
            existing = shard.stored(entry.slug)
            if existing is not None and existing.content_sha256 == digest:
                return DocumentOutcome(
                    slug=entry.slug, url=entry.url, status=UNCHANGED, kind=fetched.kind
                )
            title = fetched.title or entry.title or entry.slug
            written = self.write(entry.slug, fetched, title)
            document = StoredDocument(
                slug=entry.slug,
                url=entry.url,
                category=entry.category,
                title=title,
                kind=fetched.kind,
                filename=written.filename,
                content_sha256=digest,
                abstract=abstract_of(fetched),
                page_count=written.page_count,
                words=written.quality.metrics.words,
                published=fetched.published,
                fetched_at=now_stamp(),
                quality=written.quality,
            )
            assignment = await self.tagger.assign(
                self.declaration, document, self.store.document_path(source, document)
            )
            shard.record_document(assignment.applied_to(document))
            shard.clear_failure(entry.slug)
            return DocumentOutcome(
                slug=entry.slug, url=entry.url, status=STORED, kind=fetched.kind
            )
        except Exception as error:
            failure_class = classify(error)
            logger.warning(
                "Could not store %s/%s [%s]: %s",
                source,
                entry.slug,
                failure_class,
                error,
            )
            shard.record_failure(
                StoreFailure(
                    slug=entry.slug,
                    url=entry.url,
                    failure_class=failure_class,
                    error=f"{type(error).__name__}: {error}",
                    last_failed_at=now_stamp(),
                )
            )
            return DocumentOutcome(
                slug=entry.slug,
                url=entry.url,
                status=FAILED,
                failure_class=failure_class,
                error=f"{type(error).__name__}: {error}",
            )

    async def store_all(
        self, entries: list[DiscoveredEntry], shard: SourceShard
    ) -> list[DocumentOutcome]:
        """Store every selected document, a few at a time."""
        limiter = asyncio.Semaphore(self.concurrency)

        async def guarded(entry: DiscoveredEntry) -> DocumentOutcome:
            async with limiter:
                return await self.store_one(entry, shard)

        return list(await asyncio.gather(*(guarded(entry) for entry in entries)))

    async def run(self, pages: PageCache) -> SourceReport:
        """Enumerate, fetch what the index does not already hold, and report."""
        declaration = self.declaration
        shard = self.store.load(declaration)
        known_before = {entry.slug for entry in shard.discovered}

        outcome = await discover_avenues(declaration.avenues, pages)
        merged_discovery(shard, outcome)
        fresh = sum(1 for item in outcome.items if item.slug not in known_before)

        work = pending_entries(shard, refresh=self.refresh)
        selected = work[: self.limit] if self.limit else work
        outcomes = await self.store_all(selected, shard)

        shard.updated_at = now_stamp()
        self.store.save(shard)

        return SourceReport(
            source=declaration.key,
            display_name=declaration.display_name,
            active=declaration.active,
            discovered=sum(1 for entry in shard.discovered if entry.present),
            new=fresh,
            stored=sum(1 for one in outcomes if one.status == STORED),
            unchanged=sum(1 for one in outcomes if one.status == UNCHANGED),
            skipped=len(work) - len(selected),
            failed=sum(1 for one in outcomes if one.status == FAILED),
            crawl_degraded=shard.crawl_degraded,
            crawl_suspect=shard.crawl_suspect,
            avenue_failures=outcome.failures,
            failures=tuple(one for one in outcomes if one.status == FAILED),
        )


async def ingest_source(
    declaration: SourceDeclaration,
    store: CorpusStore,
    fetcher: DocumentFetcher,
    *,
    pages: PageCache | None = None,
    tagger: DocumentTagger | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int = 0,
    refresh: bool = False,
) -> SourceReport:
    """Ingest one source, turning any failure of the whole into a report.

    Nothing raises out of here. A source whose enumeration collapses entirely —
    a moved sitemap, a host that no longer resolves — is reported as aborted so
    the run continues and an operator can see which source to look at.
    """
    reader = HttpPageReader(fetcher.client, declaration, profile=fetcher.profile)
    cache = pages if pages is not None else PageCache(reader=reader)
    ingestor = SourceIngestor(
        declaration=declaration,
        store=store,
        fetcher=fetcher,
        tagger=tagger if tagger is not None else DocumentTagger(),
        concurrency=concurrency,
        limit=limit,
        refresh=refresh,
    )
    try:
        return await ingestor.run(cache)
    except Exception as error:
        logger.exception("Ingestion of %s aborted", declaration.key)
        return SourceReport(
            source=declaration.key,
            display_name=declaration.display_name,
            active=declaration.active,
            aborted=f"{classify(error)}: {type(error).__name__}: {error}",
        )


async def ingest_with(
    declarations: tuple[SourceDeclaration, ...],
    store: CorpusStore,
    fetcher: DocumentFetcher,
    *,
    tagger: DocumentTagger | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int = 0,
    refresh: bool = False,
) -> IngestReport:
    """Ingest several sources on a fetcher the caller owns.

    Each source gets its own page cache, so one source's unreachable sitemap is
    not remembered as a failure for the next — they share nothing but the client.
    """
    started = now_stamp()
    reports = [
        await ingest_source(
            declaration,
            store,
            fetcher,
            tagger=tagger,
            concurrency=concurrency,
            limit=limit,
            refresh=refresh,
        )
        for declaration in declarations
    ]
    report = IngestReport(
        sources=tuple(reports), started_at=started, finished_at=now_stamp()
    )
    logger.info("Corpus run finished\n%s", report.summary())
    return report


async def ingest_sources(
    declarations: tuple[SourceDeclaration, ...],
    store: CorpusStore,
    *,
    profile: str | None = None,
    tagger: DocumentTagger | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int = 0,
    refresh: bool = False,
) -> IngestReport:
    """Ingest several sources in turn, one source's failure costing only itself."""
    async with corpus_client() as client:
        return await ingest_with(
            declarations,
            store,
            DocumentFetcher(client=client, profile=profile),
            tagger=tagger,
            concurrency=concurrency,
            limit=limit,
            refresh=refresh,
        )


class UnknownSource(Exception):
    """A source that no declaration names."""


def resolve_sources(keys: tuple[str, ...]) -> tuple[SourceDeclaration, ...]:
    """The declarations named by ``keys``, or every active one when none are.

    Naming a source explicitly reaches it whether or not it is active, because
    asking for it by name is the deliberate act its declaration was waiting for.
    """
    if not keys:
        return active_declarations()
    found = [declaration_for(key) for key in keys]
    missing = [key for key, entry in zip(keys, found) if entry is None]
    if missing:
        known = ", ".join(entry.key for entry in DECLARED_SOURCES)
        raise UnknownSource(f"No such source: {', '.join(missing)}. Declared: {known}")
    return tuple(entry for entry in found if entry is not None)


async def sync_corpus(
    store: CorpusStore,
    *,
    keys: tuple[str, ...] = (),
    profile: str | None = None,
    tagger: DocumentTagger | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int = 0,
    refresh: bool = False,
) -> IngestReport:
    """Ingest the named sources, or every active one, into ``store``."""
    return await ingest_sources(
        resolve_sources(keys),
        store,
        profile=profile,
        tagger=tagger,
        concurrency=concurrency,
        limit=limit,
        refresh=refresh,
    )
