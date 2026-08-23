"""Reading one document down to what it establishes, once and for all time.

The corpus de-duplicates documents; nothing de-duplicated conclusions. One
subsection's research spent $27 and 32M input tokens, and almost none of it
went on *finding* things — 42 of its 120 sources came off disk, and every
"what happened this year" question was answered from the corpus. It went on
**reading** them. The next subsection reopens the same evaluations paper and
re-derives the same numbers at full price, and the one after that does it
again.

**Keyed by what it is about, never by the run that needed it.** A document's
findings are a function of its bytes, so the cache key is ``content_sha256``
and nothing else. A document that changes source, gets re-slugged, or is
fetched again unchanged reuses what was read of it; two sources holding the
same paper read it once between them. That is also what keeps a single part
optional: one part pays for its own keys, a full pass warms all of them, and
neither has to know the other exists.

**It cannot ride the judging pass.** The judge reads shallow on purpose —
:data:`PDF_JUDGING_PAGES` of a hundred-page paper, because the front matter is
what says what a document is about — and it is answering a different question:
what subject is this. A finding is a number with a caveat attached and it is on
page 34. So this is a fourth stage after fetch, judge, and embed, reading the
whole document rather than its opening.

**Lazy, because most of the corpus is not on any subject anybody is writing
about.** A document is distilled the first time a filter reaches it. Eleven
hundred documents distilled up front would be a bill for a corpus of which one
subsection needs three hundred; distilled as they are reached, the bill arrives
in the order the writing does and stops when the writing stops.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.corpus.retrieval import CorpusEntry
from inkwell.corpus.storage import CorpusStore, now_stamp

logger = logging.getLogger(__name__)

FINDINGS_DIR = "findings"
"""Where distillations sit: under the corpus root, beside the sources.

Not inside a source's directory, because the key is the document's content and
a document is not owned by the source that happened to serve it. Two sources
holding the same paper share one file here, and a document that moves between
them keeps its findings.
"""

DEFAULT_DISTIL_CONCURRENCY = 4
"""How many documents are read at once. Each is a delegated session reading a
whole document, so this bounds sessions rather than requests."""

DISTIL_STAGE = "research"
"""Which stage's model tier a distillation runs at.

A researcher, and billed as one. It is reading a paper for the numbers a writer
will cite and the caveats their authors attach, which is the research stage's
own job done once instead of once per part.
"""

DISTIL_SYSTEM = """\
You are reading one document down to what a writer could cite it for.

Read the whole thing. Not the abstract — a document's abstract says what it is \
about, and you are being asked what it *establishes*, which is on page 34 as \
often as on page 1.

Report one finding per thing the document establishes that somebody writing \
about this subject would want to state on its authority: a measured number, a \
dated event, a named case, a rate, a result, a refutation. For each one:

- **The claim**, in one sentence, as the document supports it and no further. \
"Cyber-range performance roughly doubled every five months over the evaluated \
period" is a finding; "AI is getting better at cyber" is not.
- **The caveat its own authors attach.** A number reported without the \
conditions its authors put on it is how a careful paper becomes an overclaim \
two citations later, and the caveat is in the document — you are reading it, \
and nothing downstream will be.
- **A verbatim quote** carrying the claim, so a writer can quote rather than \
paraphrase.
- **Where it is** — page, section, table, or figure number.

Report nothing where the document establishes nothing citable. A survey of \
other people's results establishes that the survey exists; the results are \
theirs. Padding this list makes every use of it worse, because the whole point \
is that nobody reads the document again.
"""


class Finding(BaseModel):
    """One thing a document establishes, at the depth a writer would cite it.

    The caveat is a field rather than part of the claim because it is the half
    that gets lost: a number travels and the conditions on it do not, and a
    writer handed the pair has to decide to drop the caveat rather than never
    seeing it.
    """

    model_config = ConfigDict(frozen=True)

    claim: str = Field(
        description="What the document establishes, in one sentence, as far as "
        "it supports it and no further"
    )
    caveat: str = Field(
        default="",
        description="The conditions the document's own authors put on it, in "
        "their terms — empty only where they state none",
    )
    quote: str = Field(
        default="",
        description="A verbatim sentence from the document carrying the claim, "
        "so a writer can quote rather than paraphrase",
    )
    locator: str = Field(
        default="",
        description="Where in the document it is — page, section, table, or "
        "figure number",
    )

    def render(self) -> str:
        """This finding as a line something reading a list of them sees."""
        caveated = f" (caveat: {self.caveat})" if self.caveat else ""
        placed = f" [{self.locator}]" if self.locator else ""
        return f"{self.claim}{caveated}{placed}"


class Distillation(BaseModel):
    """Everything one document establishes, and what it was read from.

    Carries the document's identity beside its findings rather than leaving
    that to whoever holds the file, because a finding reaches a writer as
    something to cite and a citation needs a title, a venue, and a URL. Keyed
    by content, so the source and slug recorded here are where it was read
    *that time* and not a claim about where it lives now.
    """

    model_config = ConfigDict(frozen=True)

    content_sha256: str = Field(description="The bytes this was read from")
    source: str = Field(default="", description="Source it was read under")
    slug: str = Field(default="", description="Identifier within that source")
    title: str = Field(default="", description="Document title")
    url: str = Field(default="", description="Where it was published")
    published: str = Field(default="", description="The date the source stated")
    tags: tuple[str, ...] = Field(
        default=(), description="Tags it carried when it was read"
    )
    findings: tuple[Finding, ...] = Field(
        default=(), description="What it establishes, in the order it does"
    )
    distilled_at: str = Field(default="", description="When it was read")
    failure: str = Field(
        default="", description="Why it holds no findings, where it holds none"
    )

    def read(self) -> bool:
        """Whether this is a reading rather than a record of one that failed."""
        return not self.failure

    def cited(self) -> str:
        """How a writer would name this document."""
        dated = f", {self.published}" if self.published else ""
        return f"{self.title or self.slug}{dated} — {self.url}"


class DistilRequest(BaseModel):
    """One document put to a distiller, and how to open it.

    Carries the locator rather than the text for the same reason the tagging
    request does: a stored document may be a hundred-page PDF, read at a page
    range rather than transcribed. Unlike tagging, the whole document is asked
    for — the point of this stage is the page the judging never opened.
    """

    model_config = ConfigDict(frozen=True)

    slug: str = ""
    title: str = ""
    url: str = ""
    source: str = ""
    reading: str = ""
    published: str = ""
    tags: tuple[str, ...] = ()

    def prompt(self) -> str:
        """What the distiller is asked, for this document."""
        return "\n".join(
            [
                f"Document: {self.title or self.slug}",
                f"Published at: {self.url}" if self.url else "",
                f"Published on: {self.published}" if self.published else "",
                f"Source: {self.source}" if self.source else "",
                f"Subjects: {', '.join(self.tags)}" if self.tags else "",
                "",
                self.reading,
                "",
                "Read all of it, then report what it establishes.",
            ]
        )


def request_for(entry: CorpusEntry) -> DistilRequest:
    """What the distiller is shown of one stored document."""
    document = entry.document
    return DistilRequest(
        slug=document.slug,
        title=document.title,
        url=document.url,
        source=entry.display_name or entry.source,
        reading=entry.reading(),
        published=document.published,
        tags=document.tags.applied(),
    )


class Distiller(ABC):
    """Who reads a document down to what it establishes.

    A seam, only ever injected: nothing here constructs one for itself, so a
    test hands over a distiller that reads the fixture and a run hands over one
    that spends a model.
    """

    @abstractmethod
    async def distil(self, request: DistilRequest) -> tuple[Finding, ...] | None:
        """Read one document, or return None where it could not be read."""


class ModelDistiller(Distiller):
    """One whole-document reading, bought once per distinct document.

    Affordable because it is once: a corpus of low thousands, read as the
    writing reaches each document, against a per-part research stage that was
    re-reading the same papers at every part.

    ``model`` empty resolves the tier at call time rather than at construction,
    so a distiller built once still follows the settings a session later picks.
    """

    def __init__(self, model: str = "") -> None:
        self.model = model

    async def distil(self, request: DistilRequest) -> tuple[Finding, ...] | None:
        from inkwell.agent.client import query
        from inkwell.agent.config import stage_model

        class ReadDocument(BaseModel):
            findings: list[Finding] = Field(
                description="Everything this document establishes that a writer "
                "could cite it for, empty where it establishes nothing citable"
            )

        read = await query(
            request.prompt(),
            output_type=ReadDocument,
            model=self.model or stage_model(DISTIL_STAGE),
            system_prompt=DISTIL_SYSTEM,
            tools=["Read"],
            autonomy="unattended",
            prefix="distil",
        )
        return tuple(read.findings) if read is not None else None


def findings_dir(store: CorpusStore) -> Path:
    """Where every distillation the corpus holds sits."""
    return store.root / FINDINGS_DIR


def findings_path(store: CorpusStore, digest: str) -> Path:
    """The one file a document's findings live in, named by its content."""
    return findings_dir(store) / f"{digest}.json"


def read_distillation(store: CorpusStore, digest: str) -> Distillation | None:
    """What was read of one document, or nothing where it has not been read.

    An unreadable file reads as absent rather than raising: it is a cache of a
    reading, so the repair is to read again, and a caller with the document to
    hand should do that instead of being stopped.
    """
    path = findings_path(store, digest)
    if not path.is_file():
        return None
    try:
        return Distillation.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        logger.warning("Unreadable distillation at %s", path, exc_info=True)
        return None


def write_distillation(store: CorpusStore, held: Distillation) -> Path:
    """Write one document's findings, replacing whatever was there."""
    path = findings_path(store, held.content_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(held.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def distilled(store: CorpusStore) -> tuple[str, ...]:
    """Every document content the corpus has read, by digest."""
    held = findings_dir(store)
    if not held.is_dir():
        return ()
    return tuple(sorted(path.stem for path in held.glob("*.json")))


def by_content(entries: Iterable[CorpusEntry]) -> tuple[CorpusEntry, ...]:
    """These entries with each distinct content once, in the order first reached.

    Which entry stands for a repeated content does not matter: they are the
    same bytes by definition, so the reading is the same whichever of them is
    opened. What matters is that a filter reaching one paper under two sources
    prices and reads it once.
    """
    found = tuple(entry for entry in entries if entry.document.content_sha256)
    order = tuple(dict.fromkeys(entry.document.content_sha256 for entry in found))
    standing = {entry.document.content_sha256: entry for entry in found}
    return tuple(standing[digest] for digest in order)


def awaiting_distillation(
    store: CorpusStore, entries: Iterable[CorpusEntry]
) -> tuple[CorpusEntry, ...]:
    """The documents a distillation run would read, each distinct content once.

    Separate from :func:`distil_entries` so a caller can price the work before
    starting it — which is most of what makes the reading affordable to ask
    for: an operator who can see that a filter reaches three hundred documents
    of which forty are unread is deciding about forty.

    A document with no stored body is left out rather than reported as unread.
    Nothing could read it, and counting it would price work that cannot happen.
    """
    held = distilled(store)
    return tuple(
        entry
        for entry in by_content(entries)
        if entry.document.content_sha256 not in held and entry.stored()
    )


class DistilStep(BaseModel):
    """One document read, told to whoever is watching the run.

    Reported from the run rather than counted off the corpus, for the reason
    the tagging pass reports its own: a watcher reading the store cannot tell a
    run that has read nothing from one that has read half, because both leave
    the document count alone.
    """

    model_config = ConfigDict(frozen=True)

    digest: str
    title: str
    findings: int
    done: int
    total: int
    failure: str = Field(
        default="", description="Why this reading stopped before it landed"
    )


class DistilReport(BaseModel):
    """What one distillation run did, in the terms it is asked about in."""

    model_config = ConfigDict(frozen=True)

    reached: int = Field(default=0, description="Documents the filter reached")
    already_read: int = Field(
        default=0, description="Of those, ones a previous run had already read"
    )
    read: int = Field(default=0, description="Documents this run read")
    findings: int = Field(default=0, description="Findings this run recorded")
    failed: int = Field(default=0, description="Documents it could not read")

    def summary(self) -> str:
        """One line naming what happened, for a log or a CLI."""
        return (
            f"reached {self.reached} | already read {self.already_read} | "
            f"read {self.read} | findings {self.findings} | failed {self.failed}"
        )


async def distil_entries(
    store: CorpusStore,
    entries: Iterable[CorpusEntry],
    distiller: Distiller | None = None,
    *,
    concurrency: int = DEFAULT_DISTIL_CONCURRENCY,
    started: Callable[[DistilStep], None] | None = None,
    progress: Callable[[DistilStep], None] | None = None,
) -> DistilReport:
    """Read whatever of these documents has not been read, and keep it.

    Each reading is recorded as it lands rather than at the end, so a run
    interrupted partway keeps every document it paid for. A document that
    could not be read is recorded as a failure rather than left absent, so the
    next run can tell "nothing establishes anything here" from "try again" —
    and so a document that fails every time is not re-read on every pass.
    """
    reaching = by_content(entries)
    pending = awaiting_distillation(store, reaching)
    reading = distiller if distiller is not None else ModelDistiller()
    limiter = asyncio.Semaphore(concurrency)
    done = 0

    async def read_one(entry: CorpusEntry) -> Distillation:
        """One document read, recorded, and reported the moment it lands."""
        nonlocal done
        async with limiter:
            if started is not None:
                started(
                    DistilStep(
                        digest=entry.document.content_sha256,
                        title=entry.document.title,
                        findings=0,
                        done=done,
                        total=len(pending),
                    )
                )
            try:
                found = await reading.distil(request_for(entry))
            except asyncio.CancelledError:
                if progress is not None:
                    progress(
                        DistilStep(
                            digest=entry.document.content_sha256,
                            title=entry.document.title,
                            findings=0,
                            done=done,
                            total=len(pending),
                            failure="cancelled",
                        )
                    )
                raise
            except Exception:
                logger.exception("Distilling %s could not be read", entry.name())
                found = None
        document = entry.document
        held = Distillation(
            content_sha256=document.content_sha256,
            source=entry.source,
            slug=document.slug,
            title=document.title,
            url=document.url,
            published=document.published,
            tags=document.tags.applied(),
            findings=found or (),
            distilled_at=now_stamp(),
            failure="" if found is not None else "the reading did not come back",
        )
        write_distillation(store, held)
        done += 1
        if progress is not None:
            progress(
                DistilStep(
                    digest=held.content_sha256,
                    title=held.title,
                    findings=len(held.findings),
                    done=done,
                    total=len(pending),
                    failure=held.failure,
                )
            )
        return held

    read = list(await asyncio.gather(*(read_one(one) for one in pending)))
    return DistilReport(
        reached=len(reaching),
        already_read=len(reaching) - len(pending),
        read=len(read),
        findings=sum(len(one.findings) for one in read),
        failed=sum(1 for one in read if not one.read()),
    )


def findings_for(
    store: CorpusStore, entries: Iterable[CorpusEntry]
) -> tuple[Distillation, ...]:
    """What has been read of these documents, each distinct content once.

    A document nothing has read yet is left out rather than reported empty:
    "we read this and it establishes nothing" and "nobody has read this" are
    different answers, and only the first should stop a caller looking.
    """

    def held() -> Iterator[Distillation]:
        """Each distinct content's reading, where one succeeded."""
        for entry in by_content(entries):
            found = read_distillation(store, entry.document.content_sha256)
            if found is not None and found.read():
                yield found

    return tuple(held())
