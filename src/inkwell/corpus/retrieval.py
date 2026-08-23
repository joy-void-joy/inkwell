"""Retrieval over the typed index: navigate its fields, then read.

At hundreds to low thousands of documents the primary way to find something is
not to search for it but to *browse*: look at what is there, narrow by subject
and source and date, and read the handful that survive. So the whole of this
module is field navigation over the index the corpus already writes — titles,
tags, venue, date, source, organization — read through the models that declare
those fields. Nothing here matches text against a serialized index, and nothing
splits or pattern-matches its way to a value a model already exposes.

Three tiers, which is the shape a corpus this size is read in:

- **browse** — titles, tags, and identifiers for a filtered set. Bodiless and
  cheap, so an agent can see what exists before spending a read on anything.
- **narrow** — the same set with each document's abstract, or the summary the
  tagging pass judged where the source published no abstract.
- **read** — where and how to read the document itself, page ranges included.
  The tier hands over a locator rather than prose: a PDF is stored as a PDF and
  is read directly, because an extraction of one is a plausible wrong answer.

Two things sit beside that navigation rather than under it. A **phrase hunt**
reads the stored Markdown bodies for the case where a particular wording is
genuinely being hunted — a fallback, never how a field question is answered,
because going to the text for something the index declares is exactly the
string-processing-over-structured-data mistake this project names. And the
**semantic layer** is optional throughout: every path here works with no vector
anywhere on disk, and a neighbour query that cannot be answered says so rather
than failing or coming back empty (see ``semantics``).

Retrieval reads the shards on disk. There is no server, no port, and no process
between an agent and the corpus.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date, datetime
from itertools import islice
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.provenance import Acquisition, Venue
from inkwell.corpus.semantics import SemanticAnswer, SemanticLayer, SemanticStatus
from inkwell.corpus.storage import (
    CorpusStore,
    DocumentKind,
    SourceShard,
    StoredDocument,
)

logger = logging.getLogger(__name__)

READ_WINDOW_PAGES = 20
"""How many pages one direct read of a PDF may ask for. A ceiling the reading
tool imposes, so a full read of a long PDF is a declared sequence of ranges
rather than one request that will be refused."""


def page_ranges(page_count: int, *, window: int = READ_WINDOW_PAGES) -> tuple[str, ...]:
    """The ranges a full read of a ``page_count``-page PDF is made of."""
    return tuple(
        f"{start}-{min(start + window - 1, page_count)}"
        for start in range(1, page_count + 1, window)
    )


class CorpusEntry(BaseModel):
    """One stored document together with what its source says about it.

    A document's own fields answer what it is; its source's fields answer who
    published it and what its word is worth. A query filters on both, so the
    pair is the unit retrieval works in rather than the document alone — and
    both halves come out of the same shard file, which is why reading one file
    per source is the whole of loading the index.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    display_name: str = ""
    organization: str = ""
    venue: str = ""
    authority: Venue = "lab_publication"
    directory: Path = Path()
    document: StoredDocument

    def name(self) -> str:
        """How this document is named across sources."""
        return f"{self.source}/{self.document.slug}"

    def path(self) -> Path:
        """Where the document itself is, the bare directory when none was stored."""
        return self.directory / self.document.filename

    def stored(self) -> bool:
        """Whether there is a body on disk to read at all."""
        return bool(self.document.filename) and self.path().is_file()

    def pages(self) -> tuple[str, ...]:
        """The page ranges a full read of this document is made of, if a PDF."""
        if not self.document.is_pdf():
            return ()
        return page_ranges(self.document.page_count)

    def reading(self) -> str:
        """The direct read this document is opened by, as a sentence.

        A locator rather than the text: an extraction sitting in a tool result
        is a copy nobody can check against the original, and for a PDF it is a
        garbled one, so the tier that answers "read this" says where and how.
        """
        if not self.stored():
            return ""
        if not self.document.is_pdf():
            return f"Read {self.path()}"
        opened = " then ".join(
            f"pages='{one}'" for one in self.pages() or (f"1-{READ_WINDOW_PAGES}",)
        )
        return (
            f"Read {self.path()} with {opened} — it is a "
            f"{self.document.page_count or 'unknown'}-page PDF, stored as a PDF "
            "because extracting it would garble its notation"
        )


def shard_entries(shard: SourceShard, directory: Path) -> tuple[CorpusEntry, ...]:
    """One shard's stored documents, paired with what the shard says of them."""
    return tuple(
        CorpusEntry(
            source=shard.source,
            display_name=shard.display_name,
            organization=shard.organization,
            venue=shard.venue,
            authority=shard.authority,
            directory=directory,
            document=document,
        )
        for document in shard.documents
    )


class CorpusIndex(BaseModel):
    """The corpus as retrieval reads it: the shards, and the entries in them.

    Both come out of one parse of each ``index.json``, which is why they are
    one model rather than two calls. A query walks the entries; the questions
    that are about a source rather than a document — how much of what it
    publishes is actually held — read the shards.
    """

    model_config = ConfigDict(frozen=True)

    shards: tuple[SourceShard, ...] = ()
    entries: tuple[CorpusEntry, ...] = ()

    def listed_but_absent(self) -> int:
        """Documents the read sources publish that the corpus has not stored.

        What tells a reader the corpus is thin *here* rather than that the
        subject is thin: a filter returning three documents means something
        different when the source lists two hundred it has never fetched.
        """
        absent = 0
        for shard in self.shards:
            stored = shard.documents_by_slug()
            absent += sum(
                1
                for entry in shard.discovered
                if entry.present and entry.slug not in stored
            )
        return absent


def read_index(store: CorpusStore, sources: tuple[str, ...] = ()) -> CorpusIndex:
    """Every stored document the corpus holds, read straight from the shards.

    Reading files is the whole of it — pydantic parses each source's
    ``index.json`` and a query walks the models. Nothing has to be running for
    this to answer, which is why the port source's API surface is the one part
    of it deliberately left behind.
    """
    read = tuple(
        shard
        for key in store.sources()
        if not sources or key in sources
        if (shard := store.load_by_name(key)) is not None
    )
    return CorpusIndex(
        shards=read,
        entries=tuple(
            entry
            for shard in read
            for entry in shard_entries(shard, store.source_dir(shard.source))
        ),
    )


def as_day(stamp: str) -> date | None:
    """The calendar day ``stamp`` names, or None where it names none.

    Parsed rather than sliced: an index carries dates in two shapes — a bare
    ``2025-06-02`` a publisher stated, and a ``2025-06-02T10:00:00Z`` an
    ingestion run stamped — and reading the day out of either is what
    ``fromisoformat`` is for.
    """
    try:
        return datetime.fromisoformat(stamp).date()
    except ValueError:
        return None


class CorpusFilter(BaseModel):
    """Which documents a query is about, stated in the index's own fields.

    Every field here is one the index declares, read through the model that
    declares it. ``title_contains`` is the single text test and it tests the
    title field itself — a field question, not a hunt through prose; for the
    latter there is the phrase hunt, deliberately named apart.

    Several filters given at once all have to hold: narrowing is what a browse
    at this scale is made of, so ``tags`` plus ``since`` plus ``venues`` reads
    as one question rather than as three alternatives.
    """

    model_config = ConfigDict(frozen=True)

    sources: tuple[str, ...] = Field(
        default=(), description="Source keys to look in (default: every source held)"
    )
    organizations: tuple[str, ...] = Field(
        default=(), description="Publishing organizations, as the registry names them"
    )
    venues: tuple[str, ...] = Field(
        default=(), description="Publication venues a citation would name"
    )
    authorities: tuple[Venue, ...] = Field(
        default=(),
        description=(
            "Standing, in the venue vocabulary — lab_publication, "
            "official_report, peer_reviewed, and the rest"
        ),
    )
    categories: tuple[str, ...] = Field(
        default=(), description="Sections of a source its documents fall under"
    )
    tags: tuple[str, ...] = Field(
        default=(),
        description=(
            "Tags a document must carry — all of them, spelled as the "
            "vocabulary spells them"
        ),
    )
    title_contains: tuple[str, ...] = Field(
        default=(), description="Words a title must contain, all of them, any case"
    )
    since: str = Field(default="", description="On or after this date, YYYY-MM-DD")
    until: str = Field(default="", description="On or before this date, YYYY-MM-DD")
    kinds: tuple[DocumentKind, ...] = Field(
        default=(), description="'markdown' or 'pdf'"
    )
    min_quality: float = Field(
        default=0.0, description="Lowest recorded quality score to keep, 0-1"
    )
    untagged_only: bool = Field(
        default=False,
        description=(
            "Only documents a tagging pass read and found nothing to say about "
            "— the corpus's own blind spot, reachable by source and nothing else"
        ),
    )

    def dated_within(self, document: StoredDocument) -> bool:
        """Whether this document falls inside the asked-for date bounds."""
        if not self.since and not self.until:
            return True
        day = as_day(document.recency())
        if day is None:
            return False
        after = as_day(self.since)
        before = as_day(self.until)
        return (after is None or day >= after) and (before is None or day <= before)

    def titled(self, document: StoredDocument) -> bool:
        """Whether every asked-for word appears in the document's title."""
        title = document.title.casefold()
        return all(word.casefold() in title for word in self.title_contains)

    def named(self, asked: tuple[str, ...], recorded: str) -> bool:
        """Whether a recorded name is one of the ones asked for, in any case."""
        return not asked or any(recorded.casefold() == one.casefold() for one in asked)

    def keeps(self, entry: CorpusEntry) -> bool:
        """Whether ``entry`` survives every filter this query declared."""
        document = entry.document
        return (
            (not self.sources or entry.source in self.sources)
            and self.named(self.organizations, entry.organization)
            and self.named(self.venues, entry.venue)
            and (not self.authorities or entry.authority in self.authorities)
            and (not self.categories or document.category in self.categories)
            and all(document.tags.carries(tag) for tag in self.tags)
            and self.titled(document)
            and (not self.kinds or document.kind in self.kinds)
            and document.quality.score >= self.min_quality
            and (not self.untagged_only or document.tags.untagged())
            and self.dated_within(document)
        )

    def apply(self, entries: tuple[CorpusEntry, ...]) -> tuple[CorpusEntry, ...]:
        return tuple(entry for entry in entries if self.keeps(entry))


type OrderName = Literal["recency", "quality", "title", "source"]
"""An axis results may be placed along. Adding one means a new ``OrderKey``
below and its name here, so a key a caller cannot ask for does not compile."""

QUALITY_WIDTH = 7
"""Digits a recorded score is rendered in when it is used as a sort key —
enough for a bounded 0-to-1 score to read the same width every time, which is
what makes letter-by-letter order numeric order."""


class OrderKey(BaseModel, ABC):
    """One axis results may be placed along.

    ``place`` answers in strings rather than in each axis's own type because a
    sort compares tuples element by element, and a tuple mixing a date with a
    float has no order at all. A string is the one form every axis has here:
    ISO-8601 is chronological by construction, and a bounded score rendered at
    a fixed width is numeric order read letter by letter.

    A declared union rather than a switch: a new axis is one class, and no walk
    anywhere has to notice it. Abstract without naming ``ABC``, as the other
    declared unions in this package are — pydantic's metaclass is an
    ``ABCMeta``, so the abstract method below is enforced already.
    """

    model_config = ConfigDict(frozen=True)

    name: OrderName
    description: str

    @abstractmethod
    def place(self, entry: CorpusEntry) -> str:
        """Where ``entry`` sits on this axis, as a string that sorts by it."""


class RecencyKey(OrderKey):
    name: OrderName = "recency"
    description: str = (
        "When the document is from — its own publication date where the source "
        "stated one, and when it was fetched where none was stated"
    )

    def place(self, entry: CorpusEntry) -> str:
        return entry.document.recency()


class QualityKey(OrderKey):
    name: OrderName = "quality"
    description: str = "The capture's recorded quality score"

    def place(self, entry: CorpusEntry) -> str:
        return f"{entry.document.quality.score:0{QUALITY_WIDTH}.4f}"


class TitleKey(OrderKey):
    name: OrderName = "title"
    description: str = "The document's title, alphabetically"

    def place(self, entry: CorpusEntry) -> str:
        return entry.document.title.casefold()


class SourceKey(OrderKey):
    name: OrderName = "source"
    description: str = "Which source published it, so one source's run stays together"

    def place(self, entry: CorpusEntry) -> str:
        return entry.source


ORDER_KEYS: tuple[OrderKey, ...] = (
    RecencyKey(),
    QualityKey(),
    TitleKey(),
    SourceKey(),
)
"""Every axis a caller may order by, declared once and offered by name."""


def order_key(name: OrderName) -> OrderKey:
    """The declared axis ``name`` asks for."""
    return next(key for key in ORDER_KEYS if key.name == name)


class Ordering(BaseModel):
    """How results are placed in order — a policy, declared and overridable.

    Most recent first, with the recorded quality assessment breaking ties, is
    the default because currency is the common question of a corpus that is
    topped up: what a lab published last month is usually what a piece being
    written now is about, and between two documents of the same date the better
    capture is the one worth reading first. A caller that wants a different
    question answered passes different keys, or the same keys the other way up.
    """

    model_config = ConfigDict(frozen=True)

    keys: tuple[OrderName, ...] = ("recency", "quality")
    descending: bool = True

    def described(self) -> str:
        """How this ordering reads, for a result to say what it did."""
        direction = "descending" if self.descending else "ascending"
        return f"{', '.join(self.keys)} ({direction})"

    def place(self, entry: CorpusEntry) -> tuple[str, ...]:
        return tuple(order_key(name).place(entry) for name in self.keys)

    def applied(self, entries: tuple[CorpusEntry, ...]) -> tuple[CorpusEntry, ...]:
        return tuple(sorted(entries, key=self.place, reverse=self.descending))


DEFAULT_ORDERING = Ordering()
"""Most recent first, quality breaking ties. A default rather than a rule: it
is a field of every query, so disagreeing with it costs an argument."""


class TierBody(BaseModel):
    """What one tier shows of a document, and what it could not show.

    ``gap`` is the point of the model. A tier that has nothing for a document
    says so rather than dropping it, so a corpus that is thin on abstracts
    reads as thin instead of as a confident narrow answer over the few
    documents that happened to have one.
    """

    model_config = ConfigDict(frozen=True)

    abstract: str = ""
    summary: str = ""
    reading: str = ""
    gap: str = ""


type TierName = Literal["browse", "narrow", "read"]
"""One depth of reading. Adding a tier means a class below and its name here."""


class RetrievalTier(BaseModel, ABC):
    """One depth the index answers a question at.

    Independently callable, and cumulative only in the sense that a caller
    usually walks them in order: a browse says what exists, narrowing says what
    each one is about, and reading says where to open it. Each answers for
    itself what it can show of a document and what it cannot.
    """

    model_config = ConfigDict(frozen=True)

    name: TierName
    answers: str
    default_limit: int

    @abstractmethod
    def body(self, entry: CorpusEntry) -> TierBody:
        """What this tier shows of ``entry``, and what it could not show."""


class BrowseTier(RetrievalTier):
    """Titles, tags, and identifiers — everything except the document."""

    name: TierName = "browse"
    answers: str = (
        "what is in the corpus at all — title, tags, source, venue, date, and "
        "where each document would be read. Carries no prose, so a wide browse "
        "is cheap"
    )
    default_limit: int = 200

    def body(self, entry: CorpusEntry) -> TierBody:
        if entry.document.title:
            return TierBody()
        return TierBody(gap="no title was recorded, so only the slug names it")


class NarrowTier(RetrievalTier):
    """The abstract, or the summary standing in where a source published none."""

    name: TierName = "narrow"
    answers: str = (
        "what each document actually says — its abstract, or the summary the "
        "tagging pass judged where the source published no abstract"
    )
    default_limit: int = 25

    def body(self, entry: CorpusEntry) -> TierBody:
        document = entry.document
        if not document.abstract and not document.summary:
            return TierBody(
                gap=(
                    "no abstract and no judged summary — nothing narrows this "
                    "short of reading it"
                )
            )
        return TierBody(abstract=document.abstract, summary=document.summary)


class ReadTier(RetrievalTier):
    """Where and how to read the document itself, page ranges included."""

    name: TierName = "read"
    answers: str = (
        "where the document is and how to open it — the path, and the page "
        "ranges a PDF is read in. Hands over a locator rather than prose, so "
        "the full read goes to the document itself"
    )
    default_limit: int = 10

    def body(self, entry: CorpusEntry) -> TierBody:
        document = entry.document
        if not entry.stored():
            return TierBody(
                gap=(
                    "no body is stored — the index records that this document "
                    "exists, but nothing was captured for it. Top the source up, "
                    "or read it at its URL"
                )
            )
        return TierBody(
            abstract=document.abstract,
            summary=document.summary,
            reading=entry.reading(),
        )


TIERS: tuple[RetrievalTier, ...] = (BrowseTier(), NarrowTier(), ReadTier())
"""Every depth a caller may ask at, declared once and offered by name."""


def retrieval_tier(name: TierName) -> RetrievalTier:
    """The declared tier ``name`` asks for."""
    return next(tier for tier in TIERS if tier.name == name)


class PhraseMatch(BaseModel):
    """One line of one stored body carrying the phrase that was hunted."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(description="Source the match is in")
    slug: str = Field(description="Document the match is in")
    title: str = Field(description="That document's title")
    path: str = Field(description="File holding the line, for a direct read")
    line: int = Field(description="1-based line number in that file")
    text: str = Field(description="The line, whitespace collapsed")

    def document(self) -> str:
        """How this match names the document it is in, across sources."""
        return f"{self.source}/{self.slug}"


class PhraseHunt(BaseModel):
    """What a full-text hunt found, and what it could not look inside.

    The counts are the honesty. A source held mostly as PDFs has almost no text
    on disk to hunt through, and a hunt reporting only "no matches" would read
    as "the corpus does not discuss this" when it means "the corpus does not
    store the words of these documents at all".
    """

    model_config = ConfigDict(frozen=True)

    phrase: str = Field(description="What was hunted for")
    matches: tuple[PhraseMatch, ...] = Field(
        default=(), description="Lines carrying it, in index order"
    )
    documents: int = Field(default=0, description="Documents at least one match is in")
    searched: int = Field(default=0, description="Stored Markdown bodies actually read")
    pdfs: int = Field(
        default=0,
        description=(
            "Documents whose text is deliberately not on disk — read those "
            "directly at the page ranges the read tier gives"
        ),
    )
    unreadable: tuple[str, ...] = Field(
        default=(), description="Documents with no stored body to hunt through"
    )
    truncated: bool = Field(
        default=False, description="True when more matches were found than returned"
    )


MAX_PHRASE_MATCHES = 40
"""How many matching lines one hunt returns. A default: a hunt that fills this
is one whose phrase is too common to be worth hunting, and the caller is told
it was capped rather than left to assume it saw everything."""

SNIPPET_CHARS = 240
"""How much of a matching line comes back, centred on the phrase."""


def snippet(line: str, phrase: str, *, width: int = SNIPPET_CHARS) -> str:
    """The matching line, whitespace collapsed and cropped around the phrase."""
    collapsed = " ".join(line.split())
    if len(collapsed) <= width:
        return collapsed
    found = collapsed.casefold().find(phrase.casefold())
    start = max(0, found - width // 2)
    cropped = collapsed[start : start + width]
    return f"…{cropped}…" if start else f"{cropped}…"


def body_text(entry: CorpusEntry) -> str | None:
    """The stored Markdown of a document, or None where it could not be read."""
    try:
        return entry.path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("Could not read %s for a phrase hunt", entry.path())
        return None


def hunt_phrase(
    entries: tuple[CorpusEntry, ...], phrase: str, *, limit: int = MAX_PHRASE_MATCHES
) -> PhraseHunt:
    """Hunt a literal phrase through the stored Markdown bodies.

    The fallback, and named as one. It is for the case a field query cannot
    serve — a turn of phrase, a figure, a name no tag covers — and it is never
    how a question about a title, a tag, a venue, or a date is answered,
    because those are declared fields the index answers without opening a file.
    A literal, case-insensitive line match rather than a pattern: the caller is
    hunting words, not writing an expression.
    """
    wanted = phrase.casefold()
    readable = [entry for entry in entries if not entry.document.is_pdf()]
    on_disk = {entry.name() for entry in readable if entry.stored()}
    present = [entry for entry in readable if entry.name() in on_disk]

    def found() -> Iterator[PhraseMatch]:
        for entry in present:
            body = body_text(entry)
            if body is None:
                continue
            for number, line in enumerate(body.splitlines(), start=1):
                if wanted in line.casefold():
                    yield PhraseMatch(
                        source=entry.source,
                        slug=entry.document.slug,
                        title=entry.document.title,
                        path=str(entry.path()),
                        line=number,
                        text=snippet(line, phrase),
                    )

    capped = list(islice(found(), limit + 1))
    matches = tuple(capped[:limit])
    return PhraseHunt(
        phrase=phrase,
        matches=matches,
        documents=len({match.document() for match in matches}),
        searched=len(present),
        pdfs=sum(1 for entry in entries if entry.document.is_pdf()),
        unreadable=tuple(
            entry.name() for entry in readable if entry.name() not in on_disk
        ),
        truncated=len(capped) > limit,
    )


class CorpusHit(BaseModel):
    """One document a query matched, at the depth the query asked for."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(description="Source key; also its directory in the corpus")
    slug: str = Field(description="Identifier within that source")
    title: str = Field(description="Document title")
    tags: tuple[str, ...] = Field(default=(), description="Every tag it carries")
    category: str = Field(default="", description="Section of the source it came from")
    venue: str = Field(default="", description="Venue a citation of it should name")
    organization: str = Field(default="", description="Who published it")
    authority: Venue = Field(
        default="unknown",
        description=(
            "What kind of thing published it, in the vocabulary every other "
            "acquisition path is judged in — so a citation derives its standing "
            "rather than asserting one"
        ),
    )
    kind: str = Field(default="markdown", description="'markdown' or 'pdf'")
    published: str = Field(
        default="",
        description="The date the source stated, empty where it stated none",
    )
    dated: bool = Field(
        default=False,
        description=(
            "True when 'published' is the document's own date. False means it "
            "was placed by when the corpus fetched it instead"
        ),
    )
    fetched_at: str = Field(default="", description="When the corpus captured it")
    quality: float = Field(default=1.0, description="Recorded quality score, 0-1")
    quality_assessed: bool = Field(
        default=True, description="False for a PDF, whose text is never judged"
    )
    words: int = Field(default=0, description="Word count (0 for a PDF)")
    page_count: int = Field(default=0, description="Pages, for a PDF")
    url: str = Field(default="", description="Where it was published")
    path: str = Field(default="", description="File to Read")
    pages: tuple[str, ...] = Field(
        default=(),
        description="Page ranges a full read of a PDF is made of, one Read call each",
    )
    abstract: str = Field(
        default="", description="Opening prose — narrow tier and below"
    )
    summary: str = Field(
        default="", description="What it says, judged when it was tagged"
    )
    reading: str = Field(default="", description="The direct read that opens it")
    gap: str = Field(
        default="",
        description="What this tier could not answer for it, where it could not",
    )
    similarity: float = Field(
        default=0.0,
        description="Closeness to the asked-for question, semantic mode only",
    )

    def acquisition(self) -> Acquisition:
        """How this document reached the run, for a citation to derive venue from.

        The corpus is a research path like any other, so a document retrieved
        from it carries the same acquisition record a fetched one does and its
        venue is read off that record rather than asserted by whoever cites
        it. The published URL is what the derivation reads, which is why it is
        carried through ingestion rather than discarded once the file is
        stored.
        """
        return Acquisition(path="corpus", url=self.url, published=self.published)


def hit_for(
    entry: CorpusEntry, tier: RetrievalTier, *, similarity: float = 0.0
) -> CorpusHit:
    """``entry`` as ``tier`` shows it, locator included at every tier.

    The locator travels regardless of depth, because it is what the *next* tier
    needs: a browse result an agent decides to read already carries the path
    and the page ranges, so narrowing is a choice rather than a step it has to
    take first.
    """
    document = entry.document
    shown = tier.body(entry)
    return CorpusHit(
        source=entry.source,
        slug=document.slug,
        title=document.title,
        tags=document.tags.applied(),
        category=document.category,
        venue=entry.venue,
        organization=entry.organization,
        authority=entry.authority,
        kind=document.kind,
        published=document.published,
        dated=document.dated(),
        fetched_at=document.fetched_at,
        quality=document.quality.score,
        quality_assessed=document.quality.assessed,
        words=document.words,
        page_count=document.page_count,
        url=document.url,
        path=str(entry.path()) if document.filename else "",
        pages=entry.pages(),
        abstract=shown.abstract,
        summary=shown.summary,
        reading=shown.reading,
        gap=shown.gap,
        similarity=similarity,
    )


type QueryMode = Literal["structural", "phrase", "semantic"]
"""Which of the three ways of asking a query used."""


class CorpusQuery(BaseModel):
    """One question put to the corpus: what to keep, how deep, in what order."""

    model_config = ConfigDict(frozen=True)

    where: CorpusFilter = CorpusFilter()
    tier: TierName = "browse"
    ordering: Ordering = DEFAULT_ORDERING
    phrase: str = ""
    like: str = ""
    limit: int = 0
    offset: int = Field(default=0, ge=0)

    def mode(self) -> QueryMode:
        """Which way of asking this is. Structural unless it says otherwise."""
        if self.phrase:
            return "phrase"
        return "semantic" if self.like else "structural"

    def capped(self, tier: RetrievalTier) -> int:
        """How many results to return: the caller's limit, else the tier's."""
        return self.limit if self.limit > 0 else tier.default_limit


class CorpusAnswer(BaseModel):
    """What one query came back with, and what it could not answer."""

    model_config = ConfigDict(frozen=True)

    mode: QueryMode = Field(description="Which way of asking answered this")
    tier: TierName = Field(description="The depth these results are shown at")
    tier_answers: str = Field(
        default="", description="What that depth shows, and what it leaves out"
    )
    ordering: str = Field(default="", description="How the results were ordered")
    documents: tuple[CorpusHit, ...] = Field(
        default=(), description="The documents that matched, in order"
    )
    matched: int = Field(default=0, description="How many documents the filters kept")
    offset: int = Field(default=0, description="Zero-based position of this page")
    next_offset: int | None = Field(
        default=None,
        description="Offset for the next page, or null when this is the last page",
    )
    truncated: bool = Field(
        default=False, description="True when more matched than were returned"
    )
    gaps: int = Field(
        default=0,
        description=(
            "Returned documents this tier could not answer for — each says why "
            "in its own 'gap', and none was dropped for it"
        ),
    )
    sources_held: tuple[str, ...] = Field(
        default=(), description="Every source the corpus holds an index for"
    )
    listed_but_absent: int = Field(
        default=0,
        description=(
            "Documents the searched sources publish that are not stored yet — "
            "top the source up if the missing material matters for this question"
        ),
    )
    semantic: SemanticStatus = Field(
        default=SemanticStatus(),
        description="Whether the optional semantic layer could answer, and why not",
    )
    hunt: PhraseHunt | None = Field(
        default=None, description="What the phrase hunt found, in phrase mode"
    )


def ranked(
    entries: tuple[CorpusEntry, ...], answer: SemanticAnswer
) -> tuple[CorpusEntry, ...]:
    """``entries`` the semantic layer placed near the question, nearest first."""
    placed = {one.document() for one in answer.neighbours}
    return tuple(
        sorted(
            (entry for entry in entries if entry.name() in placed),
            key=lambda entry: answer.similarity(entry.name()),
            reverse=True,
        )
    )


async def search_corpus(
    query: CorpusQuery,
    store: CorpusStore,
    *,
    semantics: SemanticLayer | None = None,
) -> CorpusAnswer:
    """Answer one query against the corpus on disk.

    The structural path runs first and always: the filters narrow the index,
    the declared ordering places what is left, and the tier decides how much of
    each document is shown. A phrase hunt narrows that same set further by
    reading bodies, and the semantic layer reorders it — neither replaces it,
    and neither is a precondition for an answer.
    """
    held = store.sources()
    tier = retrieval_tier(query.tier)
    index = read_index(store, query.where.sources)
    entries = query.where.apply(index.entries)
    searched = query.where.sources or held

    layer = semantics if semantics is not None else SemanticLayer(store=store)
    found = SemanticAnswer(status=layer.status(searched))
    hunt: PhraseHunt | None = None
    mode = query.mode()

    match mode:
        case "phrase":
            hunt = hunt_phrase(entries, query.phrase)
            matched = {match.document() for match in hunt.matches}
            kept = query.ordering.applied(
                tuple(entry for entry in entries if entry.name() in matched)
            )
        case "semantic":
            found = await layer.nearest(query.like, searched)
            kept = (
                ranked(entries, found)
                if found.status.available
                else query.ordering.applied(entries)
            )
        case "structural":
            kept = query.ordering.applied(entries)

    limit = query.capped(tier)
    page_end = query.offset + limit
    shown = tuple(
        hit_for(entry, tier, similarity=found.similarity(entry.name()))
        for entry in islice(kept, query.offset, page_end)
    )
    return CorpusAnswer(
        mode=mode,
        tier=tier.name,
        tier_answers=tier.answers,
        ordering=(
            "similarity to the asked-for question"
            if mode == "semantic" and found.status.available
            else query.ordering.described()
        ),
        documents=shown,
        matched=len(kept),
        offset=query.offset,
        next_offset=page_end if page_end < len(kept) else None,
        truncated=page_end < len(kept),
        gaps=sum(1 for hit in shown if hit.gap),
        sources_held=held,
        listed_but_absent=index.listed_but_absent(),
        semantic=found.status,
        hunt=hunt,
    )
