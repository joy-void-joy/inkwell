"""What was established about one cited URL, kept under the URL.

A book cites the same sources over and over. Across the Atlas: 1,396 citation
instances to 785 distinct URLs, 863 of those instances to the 252 URLs cited
more than once — one Our World in Data page carries 27 of them. Checking a
reference is a fetch and a reading, and doing it per instance means paying for
the same page twenty-seven times to reach the same answer twenty-seven times.

**Keyed by what it is about, never by the run that needed it.** A URL's verdict
is a fact about that URL, so the cache key is the URL and nothing else. A single
part pays for its own references; a full pass warms all of them; the second pass
pays for whatever has been added since. Neither has to know the other exists,
and nothing has to read the book to warm the cache.

**What is cached is the reference, not the claim.** Whether a source supports
the sentence citing it is a question about that sentence, and two parts citing
one paper for two different claims are asking two different things. What is the
same for both is what the paper *is*: whether the URL still resolves, what it is
titled, who published it, and when. That is the half a book gets wrong at
scale — a dead link, or a figure attributed to the wrong report — and it is
exactly the half that does not depend on who is citing it.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from hashlib import blake2b
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.agent.provenance import Acquisition, Venue
from inkwell.corpus.storage import now_stamp

logger = logging.getLogger(__name__)

VERDICT_DIGEST_BYTES = 16
"""Length of the filename a URL is kept under.

A URL is not a filename — it carries slashes, query strings, and lengths no
filesystem takes — so the file is named by a digest of it and the URL itself is
recorded inside. Nothing cryptographic turns on this; it answers "is this the
same URL as last time".
"""

REFERENCE_STAGE = "research"
"""Which stage's model tier a reference check runs at.

A researcher, and billed as one: it is opening a page and saying what it is,
which is the cheapest thing the research stage does and the one worth doing
once for the whole book rather than once per citation.
"""

REFERENCE_SYSTEM = """\
You are checking one reference — not the claim it is cited for, which is \
somebody else's question, but the reference itself.

Open the URL and answer four things about the document you find:

- **Is it there.** A URL that 404s, redirects to a site's front page, or lands \
on a paywall with no content is a dead reference, and a book that ships one has \
told its reader to go and look at nothing.
- **What it is** — its title, as the document gives it.
- **Who published it** — the journal, lab, agency, or outlet.
- **When** — the publication date the document itself states, ISO 8601. Empty \
where it states none; never the date you fetched it.

Then say, in one sentence, what the document establishes — enough that somebody \
holding a citation of it can tell whether they have cited the right thing. A \
report about one incident that a draft cites for a different incident is the \
failure this catches, and it is invisible from the citation alone.

Where the page is not what a reference should be — a listing, a search result, \
a redirect to somewhere else entirely — say so plainly in `note` rather than \
describing whatever you landed on as though it were the source.
"""


def canonical(url: str) -> str:
    """One URL as the cache keys it, so two spellings of one page share a verdict.

    Parsed rather than trimmed: the fragment is a position within a document
    and never a different document, and a trailing slash on a path is the same
    resource in every server anybody cites. Query strings are kept, because for
    a great many sites they *are* the document.
    """
    split = urlsplit(url.strip())
    path = PurePosixPath(split.path).as_posix() if split.path else "/"
    return urlunsplit(
        (split.scheme.lower(), split.netloc.lower(), path, split.query, "")
    )


def keyed(url: str) -> str:
    """The filename one URL's verdict is kept under."""
    return blake2b(
        canonical(url).encode("utf-8"), digest_size=VERDICT_DIGEST_BYTES
    ).hexdigest()


class ReferenceVerdict(BaseModel):
    """What one cited URL turned out to be.

    ``reachable`` is separate from everything else because it is the answer
    that changes what to do: a reference that is *wrong* wants the citation
    corrected, and one that is *gone* wants an archive or a replacement, and a
    verdict that reported both as "problem with this source" would leave
    whoever reads it doing the triage again.
    """

    model_config = ConfigDict(frozen=True)

    url: str = Field(description="The URL as it was cited")
    canonical_url: str = Field(default="", description="The URL as the cache keys it")
    reachable: bool = Field(
        default=False, description="Whether anything was there to read"
    )
    title: str = Field(default="", description="What the document is called")
    organization: str = Field(default="", description="Who published it")
    published: str = Field(
        default="", description="The date the document itself states, ISO 8601"
    )
    authority: Venue = Field(
        default="unknown", description="What kind of thing published it"
    )
    establishes: str = Field(
        default="",
        description="What the document establishes, in one sentence — enough to "
        "tell whether a citation of it cited the right thing",
    )
    note: str = Field(
        default="",
        description="What is wrong with this as a reference, where something is",
    )
    checked_at: str = Field(default="", description="When it was checked")

    def sound(self) -> bool:
        """Whether this reference is one a book can ship."""
        return self.reachable and not self.note

    def render(self) -> str:
        """This verdict as the line somebody reading a reference report sees."""
        if not self.reachable:
            return f"{self.url} — nothing is there"
        named = self.title or "untitled"
        said = f" — {self.note}" if self.note else ""
        return f"{self.url} — {named}, {self.organization or 'unattributed'}{said}"


class ReferenceReader(ABC):
    """Who opens a cited URL and says what is there.

    A seam, only ever injected: nothing here constructs one for itself, so a
    test hands over a reader that answers from the fixture and a run hands over
    one that spends a model and a fetch.
    """

    @abstractmethod
    async def check(self, url: str) -> ReferenceVerdict | None:
        """Open one URL, or return None where the check could not be made."""


class ReadReference(BaseModel):
    """What a reader answers about one URL, before it is a stored verdict.

    Its own shape rather than the verdict because the URL and the time are the
    caller's to record: a reader asked to hand back the URL it was given is a
    reader that can hand back a different one.
    """

    reachable: bool = Field(
        description="Whether there was a document there to read at all"
    )
    title: str = Field(default="", description="What the document calls itself")
    organization: str = Field(
        default="", description="Who published it — the journal, lab, agency, or outlet"
    )
    published: str = Field(
        default="",
        description="The date the document itself states, ISO 8601. Empty where "
        "it states none — never the date you fetched it",
    )
    establishes: str = Field(
        default="",
        description="What the document establishes, in one sentence, so somebody "
        "holding a citation of it can tell whether they cited the right thing",
    )
    note: str = Field(
        default="",
        description="What is wrong with this as a reference, where something is. "
        "Empty where it is exactly what a reference should be",
    )


class ModelReference(ReferenceReader):
    """One reference opened and read, bought once per distinct URL.

    ``model`` empty resolves the tier at call time rather than at construction,
    so a reader built once still follows the settings a session later picks.
    """

    def __init__(self, model: str = "") -> None:
        self.model = model

    async def check(self, url: str) -> ReferenceVerdict | None:
        from inkwell.agent.client import query
        from inkwell.agent.config import stage_model

        read = await query(
            f"Check this reference: {url}",
            output_type=ReadReference,
            model=self.model or stage_model(REFERENCE_STAGE),
            system_prompt=REFERENCE_SYSTEM,
            tools=["WebFetch", "WebSearch"],
            autonomy="unattended",
            prefix="reference",
        )
        if read is None:
            return None
        return ReferenceVerdict(
            url=url,
            canonical_url=canonical(url),
            reachable=read.reachable,
            title=read.title,
            organization=read.organization,
            published=read.published,
            authority=venue_of(url),
            establishes=read.establishes,
            note=read.note,
            checked_at=now_stamp(),
        )


def venue_of(url: str) -> Venue:
    """What kind of thing published this, derived rather than asserted.

    Read off the acquisition the same way every other cited source's standing
    is, so a reference checked here and one gathered by a research tool are
    judged on one scale. A reader asked to name the standing itself would be
    asked to reproduce a rule the code already holds, and would disagree with
    it about a quarter of the time.
    """
    return Acquisition(path="url_fetch", url=url).venue()


class VerdictStore(BaseModel):
    """Every reference verdict on disk, addressed by the URL it is about.

    Holds nothing but its root, as the corpus store does: every read resolves a
    path and every write lands one file, so the layout rather than this class is
    what a reader has to understand.
    """

    model_config = ConfigDict(frozen=True)

    root: Path

    def path_for(self, url: str) -> Path:
        """The one file a URL's verdict lives in."""
        return self.root / f"{keyed(url)}.json"

    def load(self, url: str) -> ReferenceVerdict | None:
        """What was established about this URL, or nothing where it is unchecked.

        An unreadable file reads as unchecked rather than raising: it is a
        cache of a reading, so the repair is to read again.
        """
        path = self.path_for(url)
        if not path.is_file():
            return None
        try:
            return ReferenceVerdict.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (ValidationError, ValueError, OSError):
            logger.warning("Unreadable reference verdict at %s", path, exc_info=True)
            return None

    def save(self, verdict: ReferenceVerdict) -> Path:
        """Write one URL's verdict, replacing whatever was there."""
        path = self.path_for(verdict.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(verdict.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    def held(self) -> int:
        """How many URLs have been checked at all."""
        return len(list(self.root.glob("*.json"))) if self.root.is_dir() else 0


def distinct(urls: Iterable[str]) -> tuple[str, ...]:
    """These URLs with each distinct reference once, in the order first cited.

    The whole saving, stated: 1,396 citation instances across the Atlas reach
    785 references, and the difference is what a per-instance check pays for
    repeatedly and this pays for once.
    """
    return tuple(dict.fromkeys(canonical(url) for url in urls if url.strip()))


def unchecked(store: VerdictStore, urls: Iterable[str]) -> tuple[str, ...]:
    """The references a check would open, which is what it would cost.

    Separate from the checking so a caller can price it first: an operator who
    can see that a work cites 785 references of which 40 are unchecked is
    deciding about 40.
    """
    return tuple(url for url in distinct(urls) if store.load(url) is None)


def verdicts_for(
    store: VerdictStore, urls: Iterable[str]
) -> tuple[ReferenceVerdict, ...]:
    """What has been established about these references, each distinct one once."""

    def held() -> Iterator[ReferenceVerdict]:
        """Each checked reference's verdict, in the order first cited."""
        for url in distinct(urls):
            found = store.load(url)
            if found is not None:
                yield found

    return tuple(held())


def unsound(verdicts: Iterable[ReferenceVerdict]) -> tuple[ReferenceVerdict, ...]:
    """Every reference a book should not ship, in the order it was checked."""
    return tuple(one for one in verdicts if not one.sound())


DEFAULT_REFERENCE_CONCURRENCY = 6
"""How many references are opened at once.

Higher than the corpus stages' cap because a reference check is one fetch and
one short reading rather than a whole document, and lower than it could be
because these are other people's servers.
"""


class ReferenceStep(BaseModel):
    """One reference checked, told to whoever is watching the run."""

    model_config = ConfigDict(frozen=True)

    url: str
    sound: bool
    done: int
    total: int


class ReferenceReport(BaseModel):
    """What one reference check did, in the terms it is asked about in."""

    model_config = ConfigDict(frozen=True)

    cited: int = Field(default=0, description="Citation instances the work holds")
    references: int = Field(default=0, description="Distinct references among them")
    already_checked: int = Field(
        default=0, description="Of those, ones a previous check had already opened"
    )
    checked: int = Field(default=0, description="References this run opened")
    dead: int = Field(default=0, description="References with nothing behind them")
    doubted: int = Field(
        default=0, description="References that resolved but are not what they seem"
    )
    failed: int = Field(default=0, description="References the check could not make")

    def summary(self) -> str:
        """One line naming what happened, for a log or a CLI."""
        return (
            f"{self.cited} citation(s) to {self.references} reference(s) | "
            f"already checked {self.already_checked} | checked {self.checked} | "
            f"dead {self.dead} | doubted {self.doubted} | failed {self.failed}"
        )


async def check_references(
    store: VerdictStore,
    urls: Iterable[str],
    reader: ReferenceReader | None = None,
    *,
    concurrency: int = DEFAULT_REFERENCE_CONCURRENCY,
    progress: Callable[[ReferenceStep], None] | None = None,
) -> ReferenceReport:
    """Open whatever of these references has not been opened, and keep it.

    Each verdict is written as it lands rather than at the end, so a run
    interrupted partway keeps every reference it paid for. A check that could
    not be made writes nothing — unlike a distillation, where a failure is a
    fact about the document, a reference the network refused today is one to
    try again rather than one to record as dead.
    """
    cited = tuple(urls)
    references = distinct(cited)
    pending = unchecked(store, references)
    opening = reader if reader is not None else ModelReference()
    limiter = asyncio.Semaphore(concurrency)
    done = 0

    async def open_one(url: str) -> ReferenceVerdict | None:
        """One reference opened, recorded, and reported the moment it lands."""
        nonlocal done
        async with limiter:
            try:
                verdict = await opening.check(url)
            except Exception:
                logger.exception("Checking %s did not come back", url)
                verdict = None
        if verdict is not None:
            store.save(verdict)
        done += 1
        if progress is not None:
            progress(
                ReferenceStep(
                    url=url,
                    sound=verdict is not None and verdict.sound(),
                    done=done,
                    total=len(pending),
                )
            )
        return verdict

    opened = list(await asyncio.gather(*(open_one(one) for one in pending)))
    reached = [one for one in opened if one is not None]
    return ReferenceReport(
        cited=len(cited),
        references=len(references),
        already_checked=len(references) - len(pending),
        checked=len(reached),
        dead=sum(1 for one in reached if not one.reachable),
        doubted=sum(1 for one in reached if one.reachable and one.note),
        failed=len(opened) - len(reached),
    )
