"""What a research source is, and how the run came to have it.

A citation can only be judged when two independent things are known about the
source: the authority of the venue it appeared in, and the role it plays for the
claim it is cited for. They fail independently. A forum post relaying someone
else's thesis fails on both at once; a peer-reviewed paper cited for a thesis its
own authors credit to someone else fails only on the second, which is why one
authority score cannot stand in for both. So they are recorded as two axes, plus
the publication date a reader needs in order to know whether a claim is current.

Venue is derived, never asserted. Every document reaches a run through some
acquisition — a research tool, a host, the author's own source material — and
:class:`Acquisition` is that record: the tool hands it back with its results and
:class:`VenueRules` reads the venue off it, so a forum post cannot be labelled
peer-reviewed by an agent that would prefer it were. An agent may still override
the derivation for what it cannot see, but the override costs a stated reason
that is recorded beside the source.

A new acquisition path — a corpus of pre-indexed documents, a library API — is
one member of :data:`AcquisitionPath` and one entry in :data:`PATH_VENUES`,
feeding this derivation rather than standing up a second table to keep in
agreement with it.
"""

from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

type Venue = Literal[
    "peer_reviewed",
    "preprint",
    "official_report",
    "lab_publication",
    "dataset",
    "book",
    "encyclopedia",
    "forum_post",
    "blog",
    "news",
    "market",
    "source_document",
    "unknown",
]
"""Venue authority: what kind of thing published the document.

One axis of two — how much weight the venue itself lends a claim, independent of
whether this document is where the claim originated. ``unknown`` is what an
acquisition that cannot say records, which is honest rather than a guess.
"""

type EvidentialRole = Literal["primary", "commentary", "background"]
"""What a source is evidence of, for the claim it was cited for.

The other axis, and per (source, finding) rather than per source: the same
document is the primary source for its own results and commentary on everyone
else's. ``primary`` means the claim originates in this document; ``commentary``
means it relays or discusses a claim that originated elsewhere, and names whose;
``background`` means context or definition rather than evidence for a claim.
"""

type AcquisitionPath = Literal[
    "arxiv",
    "exa_search",
    "url_fetch",
    "wikipedia",
    "fred",
    "prediction_market",
    "source_document",
]
"""How a document reached the run — which research path produced it."""

UNDATED = "undated"
"""What a source genuinely carrying no publication date records in place of one.

Distinct from a date that was never recorded at all, which is an absent
:class:`SourceProvenance`: a check that wants dated sources has to be able to
tell "this has no date" from "nobody looked".
"""


def calendar_date(recorded: str) -> date | None:
    """The calendar date this text names, at year, month, or day precision.

    ``None`` for :data:`UNDATED`, for a blank field, and for anything that is not
    ISO 8601, so a caller comparing dates holds a date or nothing rather than a
    string whose shape it has to guess. A year, or a year and month, names the
    first day of the period it covers — the year a citation shows either way.
    """
    for candidate in (recorded, f"{recorded}-01", f"{recorded}-01-01"):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


def publication_year(recorded: str) -> str:
    """The year a citation shows for this recorded date, empty when there is none."""
    dated = calendar_date(recorded)
    return str(dated.year) if dated is not None else ""


def venue_note(venue: Venue) -> str:
    """The tag a bibliography carries for this venue.

    Empty for the kinds a reference list is already assumed to hold: a reader
    seeing a journal paper or a book learns nothing from being told so. The kinds
    that change how much weight a citation carries — a preprint that passed no
    review, a forum post, a market price — are named, because reading the URLs is
    how a reader would otherwise have to work it out.
    """
    match venue:
        case "preprint":
            return "preprint"
        case "official_report":
            return "official report"
        case "lab_publication":
            return "lab publication"
        case "dataset":
            return "dataset"
        case "encyclopedia":
            return "encyclopedia"
        case "forum_post":
            return "forum post"
        case "blog":
            return "blog"
        case "news":
            return "news"
        case "market":
            return "prediction market"
        case "source_document":
            return "source document"
        case "peer_reviewed" | "book" | "unknown":
            return ""


class PathVenue(BaseModel):
    """One acquisition path, and the venue the path itself settles."""

    model_config = ConfigDict(frozen=True)

    path: AcquisitionPath = Field(description="The research path")
    venue: Venue = Field(description="What everything it returns is")


class DomainVenue(BaseModel):
    """One entry of the host table: a domain, and what it publishes."""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(description="Domain name, without scheme or subdomain")
    venue: Venue = Field(description="What documents served under it are")

    def covers(self, host: str) -> bool:
        """Whether this entry answers for a host — the domain, or anything under it."""
        return host == self.domain or host.endswith(f".{self.domain}")


PATH_VENUES: list[PathVenue] = [
    PathVenue(path="arxiv", venue="preprint"),
    PathVenue(path="wikipedia", venue="encyclopedia"),
    PathVenue(path="fred", venue="dataset"),
    PathVenue(path="prediction_market", venue="market"),
    PathVenue(path="source_document", venue="source_document"),
]
"""The acquisition paths whose venue the path itself settles.

``url_fetch`` and ``exa_search`` are absent because either can return anything —
for those the host decides.
"""

PUBLISHER_HOSTS: dict[Venue, tuple[str, ...]] = {
    "peer_reviewed": (
        "nature.com",
        "science.org",
        "sciencedirect.com",
        "springer.com",
        "wiley.com",
        "pnas.org",
        "plos.org",
        "tandfonline.com",
        "cambridge.org",
        "oup.com",
        "jstor.org",
        "acm.org",
        "ieee.org",
        "nih.gov",
        "nejm.org",
        "thelancet.com",
        "bmj.com",
        "elifesciences.org",
        "frontiersin.org",
        "mdpi.com",
        "neurips.cc",
        "mlr.press",
        "aclanthology.org",
        "openreview.net",
    ),
    "preprint": (
        "arxiv.org",
        "biorxiv.org",
        "medrxiv.org",
        "ssrn.com",
        "osf.io",
        "nber.org",
        "researchsquare.com",
    ),
    "official_report": (
        "gov",
        "gov.uk",
        "mil",
        "who.int",
        "un.org",
        "oecd.org",
        "imf.org",
        "worldbank.org",
        "europa.eu",
        "iea.org",
        "ipcc.ch",
        "nato.int",
    ),
    "lab_publication": (
        "openai.com",
        "anthropic.com",
        "deepmind.com",
        "deepmind.google",
        "ai.meta.com",
        "research.google",
        "epoch.ai",
        "epochai.org",
        "rand.org",
        "brookings.edu",
        "governance.ai",
        "apolloresearch.ai",
    ),
    "dataset": (
        "fred.stlouisfed.org",
        "ourworldindata.org",
        "kaggle.com",
        "huggingface.co",
        "data.worldbank.org",
        "zenodo.org",
    ),
    "book": ("books.google.com", "gutenberg.org", "openlibrary.org"),
    "encyclopedia": (
        "wikipedia.org",
        "britannica.com",
        "plato.stanford.edu",
        "wikidata.org",
    ),
    "forum_post": (
        "lesswrong.com",
        "alignmentforum.org",
        "forum.effectivealtruism.org",
        "news.ycombinator.com",
        "reddit.com",
        "stackexchange.com",
        "stackoverflow.com",
        "twitter.com",
        "x.com",
        "bsky.app",
    ),
    "blog": (
        "substack.com",
        "medium.com",
        "wordpress.com",
        "blogspot.com",
        "github.io",
        "slatestarcodex.com",
        "astralcodexten.com",
        "overcomingbias.com",
        "gwern.net",
        "marginalrevolution.com",
    ),
    "news": (
        "nytimes.com",
        "washingtonpost.com",
        "theguardian.com",
        "bbc.com",
        "bbc.co.uk",
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "wsj.com",
        "ft.com",
        "economist.com",
        "theatlantic.com",
        "wired.com",
        "arstechnica.com",
        "theverge.com",
        "technologyreview.com",
        "axios.com",
        "politico.com",
        "vox.com",
        "npr.org",
    ),
    "market": (
        "polymarket.com",
        "manifold.markets",
        "metaculus.com",
        "kalshi.com",
        "goodjudgment.com",
    ),
}
"""Which hosts publish which kind of document, grouped the way the judgement is
made. An entry names a domain, so it answers for every subdomain the publisher
serves — ``plos.org`` covers ``journals.plos.org``, and ``gov`` covers every
agency under it."""

HOST_VENUES: list[DomainVenue] = sorted(
    (
        DomainVenue(domain=host, venue=venue)
        for venue, hosts in PUBLISHER_HOSTS.items()
        for host in hosts
    ),
    key=lambda known: -len(known.domain),
)
"""The same table, flattened for lookup and ordered most specific first.

Longest domain first, because the only two entries that can both answer for one
host are a domain and a suffix of it, and the suffix is the shorter — so
``data.worldbank.org`` is consulted before ``worldbank.org``.
"""


class VenueRules(BaseModel):
    """Which venue an acquisition implies — the mapping, as a replaceable table.

    Two lookups: the paths that settle a venue outright (arXiv serves preprints
    whatever was searched for), then the host the document came from. What
    neither answers stays :attr:`unrecognized`.

    Passed in rather than reached for, so a caller with different sources — a
    project whose material is all internal, a test pinning one host — hands over
    its own table instead of editing this module's.
    """

    model_config = ConfigDict(frozen=True)

    by_path: list[PathVenue] = Field(
        default=PATH_VENUES,
        description="Acquisition paths whose venue the path itself settles",
    )
    by_host: list[DomainVenue] = Field(
        default=HOST_VENUES,
        description="Domains and what they publish, most specific first",
    )
    unrecognized: Venue = Field(
        default="unknown",
        description="What an acquisition neither table answers records",
    )

    def venue_for(self, acquisition: "Acquisition") -> Venue:
        """The venue this acquisition implies.

        The path answers where a path settles the venue outright; otherwise the
        host does. The host table is only consulted when the path table is
        silent, which is what leaves ``url_fetch`` and ``exa_search`` to it.
        """
        for known in self.by_path:
            if known.path == acquisition.path:
                return known.venue
        host = acquisition.host()
        for published_by in self.by_host:
            if published_by.covers(host):
                return published_by.venue
        return self.unrecognized


DEFAULT_VENUE_RULES = VenueRules()
"""The derivation every research tool and the recording tool use unless a caller
passes its own."""


class Acquisition(BaseModel):
    """How one document reached this run.

    Every research tool hands one of these back with its results, so what a
    source's venue is computed from is what actually happened — the tool that
    produced the document, the host it came from, and the publication date the
    acquisition itself could report — rather than what an agent recalls about it
    afterwards.
    """

    model_config = ConfigDict(frozen=True)

    path: AcquisitionPath = Field(
        description="Which research path produced this document"
    )
    url: str = Field(
        default="",
        description=(
            "Where the document came from: the URL fetched, or the file path "
            "of the author's own source material"
        ),
    )
    published: str = Field(
        default="",
        description=(
            "Publication date the acquisition itself reported (ISO 8601), "
            "empty when this path cannot supply one"
        ),
    )

    def host(self) -> str:
        """The host this came from, empty for a local path."""
        return urlsplit(self.url).hostname or ""

    def venue(self, rules: VenueRules = DEFAULT_VENUE_RULES) -> Venue:
        """The venue this acquisition implies, by the default rules or a caller's."""
        return rules.venue_for(self)


class SourceProvenance(BaseModel):
    """The two axes and the date, as recorded for one cited source.

    Nothing here is optional, because absence is carried one level up:
    ``ResearchSource.provenance`` is ``None`` on an artifact written before
    provenance was recorded. A block that is present was recorded deliberately,
    so ``published == UNDATED`` means a source with no date rather than a
    question nobody asked.
    """

    venue: Venue = Field(
        description="Venue authority, derived from how the document was acquired"
    )
    role: EvidentialRole = Field(
        description="What this source is evidence of, for the claim it is cited for"
    )
    published: str = Field(
        description=(
            f"Publication date (ISO 8601), or {UNDATED!r} when the source carries none"
        )
    )
    acquired_via: AcquisitionPath = Field(
        description="The acquisition path the venue was derived from"
    )
    attributed_to: str = Field(
        default="",
        description="Whose claim a commentary source relays",
    )
    venue_reason: str = Field(
        default="",
        description=(
            "Why the derived venue was overridden, stated by the agent that "
            "overrode it; empty when the venue is the derived one"
        ),
    )

    def undated(self) -> bool:
        """Whether this source was recorded as carrying no publication date."""
        return self.published == UNDATED

    def year(self) -> str:
        """The publication year for a citation, empty when there is no date."""
        return publication_year(self.published)

    def asserted(self) -> bool:
        """Whether the venue was overridden rather than derived."""
        return bool(self.venue_reason)
