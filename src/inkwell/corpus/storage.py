"""Where the corpus lives on disk, and what shape makes it readable.

The layout is the interface. There is no query layer, no service, and no
process to run: one directory per source, the documents as files inside it, and
one JSON index beside them.

    <root>/<source>/index.json      the typed index for this source
    <root>/<source>/<slug>.md       a document extracted to Markdown
    <root>/<source>/<slug>.pdf      a document that was a PDF, still a PDF

That is enough to navigate without a service. ``retrieval`` reads these indexes
and answers what a document is, what it is about, and how much to trust it, and
Read opens a PDF at the pages the index points to. Hunting a particular wording
through the stored Markdown sits beside that as the fallback, not as the way in:
the index declares the fields, so a question about one is answered from the
index rather than from the prose.

**A PDF is never text-extracted for storage.** Extraction garbles notation and
layout, and a garbled copy is worse than no copy because it reads as authority.
So the bytes are kept verbatim and an agent reads them directly with a page
range, exactly as ``source_consult`` requires of a source document. The index
carries what is needed to *locate* a PDF — title, abstract, page count, URL —
and never a transcription of it. ``store_markdown`` refuses PDF bytes outright,
so the rule holds by construction rather than by remembering it.
"""

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.corpus.discovery import DiscoveryOutcome
from inkwell.corpus.quality import QualityReport
from inkwell.agent.provenance import Venue
from inkwell.corpus.registry import SourceDeclaration
from inkwell.corpus.tags import DocumentTags

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"
"""The one file per source that carries everything but the documents themselves."""

PDF_MAGIC = b"%PDF-"
"""How every PDF begins. Checked before a Markdown write, so PDF bytes cannot
reach the text path by accident."""

MAX_FILENAME_STEM = 180
"""How long a document's filename stem may be. Sweep slugs join every path
segment, which can outrun what a filesystem accepts; a longer one keeps a
readable prefix and a digest of the whole, so identity survives shortening."""

DIGEST_CHARS = 12


def now_stamp() -> str:
    """The current time, as the index records it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_digest(payload: bytes) -> str:
    """The content identity of a stored document.

    Identity is what the bytes are, not when they arrived: a re-run compares
    this to decide whether a document actually changed, so an unchanged page
    costs no write and a rewritten one is visible.
    """
    return hashlib.sha256(payload).hexdigest()


def document_stem(slug: str) -> str:
    """The filename stem for ``slug``, shortened where a filesystem would refuse."""
    if len(slug) <= MAX_FILENAME_STEM:
        return slug
    digest = hashlib.sha256(slug.encode()).hexdigest()[:DIGEST_CHARS]
    return f"{slug[: MAX_FILENAME_STEM - DIGEST_CHARS - 1]}-{digest}"


type DocumentKind = Literal["markdown", "pdf"]


class StoredDocument(BaseModel):
    """One document the corpus holds, and everything but its text.

    ``abstract`` and ``page_count`` are what make a PDF locatable without
    transcribing it: enough for a reader to decide the document is worth
    opening, and a page count so a page range is a meaningful request.

    ``abstract`` and ``summary`` are separate fields and never fill in for one
    another silently: the first is the document's own opening prose, the second
    is a judgement *about* the document. Merging them would mean an index that
    sometimes quotes and sometimes paraphrases without saying which.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    url: str
    category: str = ""
    title: str = ""
    kind: DocumentKind = "markdown"
    filename: str = ""
    content_sha256: str = ""
    abstract: str = ""
    summary: str = ""
    page_count: int = 0
    words: int = 0
    published: str = ""
    fetched_at: str = ""
    quality: QualityReport = QualityReport()
    tags: DocumentTags = DocumentTags()

    def is_pdf(self) -> bool:
        return self.kind == "pdf"

    def dated(self) -> bool:
        """Whether the document itself says when it was published."""
        return bool(self.published)

    def recency(self) -> str:
        """The date this document sorts and filters by, most recent last.

        Its own publication date where the source stated one, and when it was
        fetched where none was: an undated document is still somewhere in time,
        and dropping it out of every date-ordered browse would hide it far more
        thoroughly than placing it approximately does. Which of the two this is
        travels with the document, so a reader is never told a fetch date is a
        publication date.
        """
        return self.published or self.fetched_at


class DiscoveredEntry(BaseModel):
    """One item enumeration has seen, and when it was first and last listed.

    ``present`` going false is how a source dropping a document is recorded
    without the corpus forgetting it had one: the file and the index entry stay,
    and the writer can still cite what was published.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    url: str
    category: str = ""
    title: str = ""
    present: bool = True
    first_seen: str = ""
    last_seen: str = ""


class StoreFailure(BaseModel):
    """One document a run could not store, kept so the next run retries it.

    Counting attempts is what separates a blip from a document that will never
    arrive: the next run tries again either way, and an operator can see which
    is which.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    url: str
    failure_class: str = ""
    error: str = ""
    attempts: int = 1
    first_failed_at: str = ""
    last_failed_at: str = ""


class SourceShard(BaseModel):
    """One source's whole index: what exists, what is stored, what failed.

    Sharded per source because that is the unit everything else works in — a
    run syncs one source, a top-up tops up one source, and two sources being
    ingested at once never touch one another's file.
    """

    model_config = ConfigDict(validate_assignment=True)

    source: str
    display_name: str = ""
    organization: str = ""
    venue: str = ""
    authority: Venue = "lab_publication"
    active: bool = True
    updated_at: str = ""
    crawl_degraded: bool = False
    crawl_suspect: bool = False
    documents: list[StoredDocument] = Field(default_factory=list)
    discovered: list[DiscoveredEntry] = Field(default_factory=list)
    failures: list[StoreFailure] = Field(default_factory=list)

    def documents_by_slug(self) -> dict[str, StoredDocument]:
        return {document.slug: document for document in self.documents}

    def discovered_by_slug(self) -> dict[str, DiscoveredEntry]:
        return {entry.slug: entry for entry in self.discovered}

    def failures_by_slug(self) -> dict[str, StoreFailure]:
        return {failure.slug: failure for failure in self.failures}

    def categories(self) -> tuple[str, ...]:
        """Every category this source's stored documents fall under."""
        return tuple(sorted({document.category for document in self.documents}))

    def tags(self) -> tuple[str, ...]:
        """Every tag this source's stored documents carry — what a browse of it
        can actually narrow on, as opposed to what the vocabulary offers."""
        return tuple(
            sorted(
                {tag for document in self.documents for tag in document.tags.applied()}
            )
        )

    def untagged(self) -> tuple[StoredDocument, ...]:
        """The documents a judgement looked at and found nothing to say about."""
        return tuple(
            document for document in self.documents if document.tags.untagged()
        )

    def stored(self, slug: str) -> StoredDocument | None:
        by_slug = self.documents_by_slug()
        return by_slug[slug] if slug in by_slug else None

    def record_document(self, document: StoredDocument) -> None:
        """Add or replace one document, keeping the index one entry per slug."""
        kept = [
            existing for existing in self.documents if existing.slug != document.slug
        ]
        self.documents = sorted([*kept, document], key=lambda entry: entry.slug)

    def record_failure(self, failure: StoreFailure) -> None:
        """Record a failure, carrying forward how many times it has happened."""
        previous = self.failures_by_slug()
        attempts = (
            previous[failure.slug].attempts + 1 if failure.slug in previous else 1
        )
        first = (
            previous[failure.slug].first_failed_at
            if failure.slug in previous
            else failure.last_failed_at
        )
        kept = [existing for existing in self.failures if existing.slug != failure.slug]
        updated = failure.model_copy(
            update={"attempts": attempts, "first_failed_at": first}
        )
        self.failures = sorted([*kept, updated], key=lambda entry: entry.slug)

    def clear_failure(self, slug: str) -> None:
        """Drop a recorded failure, because the document has now been stored."""
        self.failures = [entry for entry in self.failures if entry.slug != slug]


def blank_shard(declaration: SourceDeclaration) -> SourceShard:
    """An index for a source nothing has been stored for yet."""
    return SourceShard(
        source=declaration.key,
        display_name=declaration.display_name,
        organization=declaration.organization,
        venue=declaration.venue,
        authority=declaration.authority,
        active=declaration.active,
    )


DEGRADED_FLOOR = 10
"""How many items a source must have had before an empty enumeration is read as
a failure rather than as a source that genuinely publishes little."""

SUSPECT_RATIO = 0.5
"""Below this share of what was listed last time, a crawl is flagged as partial
— it still counts, because a real cull looks the same from here."""


def merged_discovery(
    shard: SourceShard, outcome: DiscoveryOutcome, *, now: str = ""
) -> SourceShard:
    """Fold a fresh enumeration into the index, incrementally.

    Incremental means keyed on identity, not on time: an item's ``first_seen``
    survives every later run, ``last_seen`` moves, and an item that stops being
    listed is marked absent rather than deleted.

    A source that had documents and now enumerates none is treated as a failed
    crawl, not as a source that deleted everything — that is nearly always a
    network blip or an unreadable sitemap, and marking every item absent would
    quietly empty the source and drop it from the next run's work.
    """
    stamp = now or now_stamp()
    prior = shard.discovered_by_slug()
    listed_before = [entry for entry in shard.discovered if entry.present]
    found = list(outcome.items)

    if listed_before and not found:
        logger.error(
            "Discovery for %s found nothing but %d items were listed before — "
            "treating the crawl as degraded and keeping the previous index",
            shard.source,
            len(listed_before),
        )
        shard.crawl_degraded = True
        shard.updated_at = stamp
        return shard

    suspect = (
        len(listed_before) >= DEGRADED_FLOOR
        and len(found) < len(listed_before) * SUSPECT_RATIO
    )
    if suspect:
        logger.warning(
            "Discovery for %s found %d items against %d listed before — "
            "possibly a partial crawl",
            shard.source,
            len(found),
            len(listed_before),
        )

    fresh = {
        item.slug: DiscoveredEntry(
            slug=item.slug,
            url=item.url,
            category=item.category,
            title=item.title or (prior[item.slug].title if item.slug in prior else ""),
            present=True,
            first_seen=(
                prior[item.slug].first_seen
                if item.slug in prior and prior[item.slug].first_seen
                else stamp
            ),
            last_seen=stamp,
        )
        for item in found
    }
    gone = [
        entry.model_copy(update={"present": False})
        for entry in shard.discovered
        if entry.slug not in fresh
    ]

    shard.discovered = sorted(
        [*fresh.values(), *gone], key=lambda entry: (entry.category, entry.slug)
    )
    shard.crawl_degraded = False
    shard.crawl_suspect = suspect
    shard.updated_at = stamp
    return shard


def pending_entries(
    shard: SourceShard, *, refresh: bool = False
) -> list[DiscoveredEntry]:
    """Which listed items a run still has to fetch.

    Incremental by default: an item already stored with a content hash is
    skipped, so a re-run costs only what is new plus whatever failed last time.
    ``refresh`` re-fetches everything listed, and the content hash then decides
    whether anything is actually rewritten.
    """
    stored = shard.documents_by_slug()
    return [
        entry
        for entry in shard.discovered
        if entry.present
        and (
            refresh or entry.slug not in stored or not stored[entry.slug].content_sha256
        )
    ]


class CorpusStore(BaseModel):
    """The corpus on disk, addressed by source.

    Holds no state of its own beyond the root: every read resolves a path and
    every write lands a file, which is what keeps the layout — rather than this
    class — the thing a reader has to understand.
    """

    model_config = ConfigDict(frozen=True)

    root: Path

    def source_dir(self, source: str) -> Path:
        return self.root / source

    def index_path(self, source: str) -> Path:
        return self.source_dir(source) / INDEX_FILENAME

    def document_path(self, source: str, document: StoredDocument) -> Path:
        return self.source_dir(source) / document.filename

    def sources(self) -> tuple[str, ...]:
        """Every source the corpus holds an index for."""
        if not self.root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if (entry / INDEX_FILENAME).is_file()
            )
        )

    def load(self, declaration: SourceDeclaration) -> SourceShard:
        """The index for a source, or a blank one where none has been written.

        An unreadable index is reported and replaced by a blank one rather than
        stopping the run: the documents are still on disk, and a re-sync
        rebuilds the index from them.
        """
        path = self.index_path(declaration.key)
        if not path.is_file():
            return blank_shard(declaration)
        try:
            return SourceShard.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError):
            logger.warning("Unreadable corpus index at %s", path, exc_info=True)
            return blank_shard(declaration)

    def load_by_name(self, source: str) -> SourceShard | None:
        """The index under ``source``, for a reader with no declaration in hand."""
        path = self.index_path(source)
        if not path.is_file():
            return None
        try:
            return SourceShard.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError):
            logger.warning("Unreadable corpus index at %s", path, exc_info=True)
            return None

    def save(self, shard: SourceShard) -> Path:
        """Write a source's index, replacing whatever was there."""
        path = self.index_path(shard.source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shard.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    def store_markdown(self, source: str, slug: str, text: str) -> str:
        """Write one extracted document as Markdown, returning its filename.

        Refuses PDF bytes: a PDF reaching this path would mean something
        extracted it to text, which is exactly what this corpus does not do.
        """
        payload = text.encode("utf-8")
        if payload.startswith(PDF_MAGIC):
            raise ValueError(
                f"{source}/{slug} looks like a PDF; store it with store_pdf so it "
                "stays a PDF rather than an extraction of one"
            )
        filename = f"{document_stem(slug)}.md"
        target = self.source_dir(source) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return filename

    def store_pdf(self, source: str, slug: str, data: bytes) -> str:
        """Write one PDF exactly as it arrived, returning its filename."""
        filename = f"{document_stem(slug)}.pdf"
        target = self.source_dir(source) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return filename
