"""Enumerate what a source published, without fetching any of it.

Discovery answers one question — *what exists here, at what URLs* — and the
answer is a list of items, not content. What makes it enumeration rather than
search is that no query is involved: a sitemap or a listing page names every
document a source published, so the corpus can hold all of them.

An **avenue** is one publishing surface of one source, and it is *data*: a
declaration in ``registry`` picks an avenue type and fills in its fields, so
adding a source adds entries rather than a module. Each avenue type knows how
to walk its own shape — a sitemap, a paginated listing, a listing table, or a
whole-domain sweep — and reports items in the same form.

Identity is derived from the URL, never from when a fetch happened:
``source_id_for`` maps a URL to a slash-free, cross-domain-unique slug, so the
same document discovered on two runs is recognisably the same document and an
incremental run can tell what is genuinely new. Every enumeration here
accumulates into a slug-keyed map for the same reason: the key *is* the
identity, so de-duplication and "have we seen this" are one lookup.
"""

import gzip
import ipaddress
import logging
import string
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from html.parser import HTMLParser
from itertools import groupby
from pathlib import PurePosixPath
from urllib import robotparser
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from inkwell.corpus.tags import TagTerm

logger = logging.getLogger(__name__)


class DiscoveredItem(BaseModel):
    """One document a source publishes, as enumeration found it.

    ``title`` is filled only where the listing exposes it and the slug does
    not — hash-named PDFs are the case that matters, since their filename
    carries nothing a reader could use.
    """

    model_config = ConfigDict(frozen=True)

    category: str
    slug: str
    url: str
    title: str = ""


# ── URL identity ──────────────────────────────────────────────────────────────


def normalize_url(url: str) -> str:
    """The de-duplication key for a URL, stable across a source's domains.

    Drops the fragment, strips one trailing slash (never from a bare host),
    lowercases scheme and host, and percent-decodes the path. The query is
    kept, because on some policy and listing pages it is what selects the
    document.
    """
    without_fragment, _ = urldefrag(url.strip())
    parsed = urlparse(without_fragment)
    path = unquote(parsed.path)
    if len(path) > 1:
        path = path.removesuffix("/")
    rebuilt = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    return f"{rebuilt}?{parsed.query}" if parsed.query else rebuilt


SLUG_SAFE = string.ascii_letters + string.digits + "._-"
"""Characters a slug may carry, so it is safe as a filename and as a JSON key."""

HOST_LABEL_SAFE = string.ascii_letters + string.digits + "-"
"""Characters a host contributes to a slug: dots become dashes, so a subdomain
label reads as one word rather than as a nested path."""


def slugified(text: str, *, safe: str = SLUG_SAFE) -> str:
    """``text`` with every run of characters outside ``safe`` become one dash."""
    swapped = (char if char in safe else "-" for char in text)
    collapsed = "".join(
        "-" if char == "-" else "".join(run) for char, run in groupby(swapped)
    )
    return collapsed.removeprefix("-").removesuffix("-")


def path_segments(url: str) -> tuple[str, ...]:
    """The non-empty path segments of a URL, percent-decoded."""
    path = PurePosixPath(unquote(urlparse(url).path))
    return tuple(part for part in path.parts if part != "/")


def host_of(url: str) -> str:
    """A URL's hostname, lowercased and free of port or brackets."""
    return (urlparse(url).hostname or "").lower()


def source_id_for(url: str, apex: str = "") -> str:
    """A deterministic, slash-free, cross-domain-unique id for a URL.

    Path segments join with dashes, and a host that is not ``apex`` adds a
    ``<label>__`` prefix, so the same path served from two subdomains cannot
    collide. An empty ``apex`` means the URL's own host is the reference, which
    is right whenever a source publishes on one domain. Derived from the URL
    alone, which is what lets a later run recognise a document it stored.
    """
    host = host_of(url)
    segments = path_segments(url)
    base = slugified("-".join(segments)) if segments else "index"
    base = base or "index"

    if not apex or host in (apex, f"www.{apex}"):
        return base
    label = slugified(host.removesuffix(f".{apex}"), safe=HOST_LABEL_SAFE)
    return f"{label}__{base}" if label else base


# ── Reading pages ─────────────────────────────────────────────────────────────


class PageReader(ABC):
    """Where a page's bytes come from.

    A seam, only ever injected: discovery never constructs one, it is handed a
    ``PageCache`` composed over whichever reader the caller chose — live HTTP
    in a run, a fixture in a test.
    """

    @abstractmethod
    async def read(self, url: str) -> bytes:
        """The bytes at ``url``, raising on any failure to get them."""


class CachedPage(BaseModel):
    """What one page turned out to be: its bytes, or the failure instead.

    Both outcomes are worth remembering. A sitemap several avenues share
    should cost one fetch, and one that is down should cost one attempt —
    without the failure recorded, every avenue wanting it would retry.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    data: bytes = b""
    error: Exception | None = None


class PageCache(BaseModel):
    """The pages one discovery run has already asked for.

    Sources point several avenues at one sitemap — Epoch's four categories
    share two files — so without this a run would fetch the same XML once per
    category.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    reader: PageReader
    pages: dict[str, CachedPage] = Field(default_factory=dict)

    async def fetch(self, url: str) -> CachedPage:
        """Read one page, recording a failure rather than raising it."""
        try:
            return CachedPage(data=await self.reader.read(url))
        except Exception as exc:
            return CachedPage(error=exc)

    async def read(self, url: str) -> bytes:
        if url not in self.pages:
            self.pages[url] = await self.fetch(url)
        cached = self.pages[url]
        if cached.error is not None:
            raise cached.error
        return cached.data

    async def read_text(self, url: str) -> str:
        return (await self.read(url)).decode("utf-8", "replace")


# ── Sitemaps ──────────────────────────────────────────────────────────────────

DOCTYPE_DECLARATION = b"<!DOCTYPE"
"""Refused outright in a sitemap, since it is the only way to declare the
entities that make expansion attacks possible and no sitemap needs one."""

MAX_SITEMAP_DEPTH = 5
"""How deep a sitemap index may nest before a run stops following it."""


def gunzipped(url: str, data: bytes) -> bytes:
    """``data``, decompressed when the URL or its magic bytes say it is gzip."""
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def xml_root(data: bytes) -> ElementTree.Element:
    """Parse third-party XML, refusing anything that declares a DTD.

    Entity-expansion attacks need declared entities, declared entities need a
    DOCTYPE, and a sitemap has no legitimate reason to carry one — so refusing
    the declaration removes the whole class without a hardened parser.
    """
    if DOCTYPE_DECLARATION in data.upper():
        raise ValueError("XML declaring a DTD is refused")
    return ElementTree.fromstring(data)


NAMESPACE_CLOSE = "}"
"""What ends the ``{uri}`` prefix ElementTree puts on a namespaced tag; it
cannot occur in a local name, so the name begins just after it."""


def local_name(tag: str) -> str:
    """An element's name without its namespace."""
    if NAMESPACE_CLOSE not in tag:
        return tag
    return tag[tag.index(NAMESPACE_CLOSE) + 1 :]


class SitemapPage(BaseModel):
    """One parsed sitemap: whether it indexes others, and what it lists."""

    model_config = ConfigDict(frozen=True)

    is_index: bool
    locations: tuple[str, ...]


def sitemap_page(data: bytes) -> SitemapPage:
    """Read a sitemap's ``<loc>`` values, namespace-agnostically."""
    root = xml_root(data)
    return SitemapPage(
        is_index=local_name(root.tag) == "sitemapindex",
        locations=tuple(
            element.text.strip()
            for element in root.iter()
            if local_name(element.tag) == "loc"
            and element.text
            and element.text.strip()
        ),
    )


PRIVATE_HOSTS: tuple[str, ...] = ("localhost", "0.0.0.0")
"""Build-time hosts a static-site generator may leak into a sitemap."""


def is_private_host(host: str) -> bool:
    """Whether a host is a build-time artifact rather than a public origin."""
    if host in PRIVATE_HOSTS or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def rehosted(location: str, origin: str) -> str:
    """A dev-leaked location moved onto the origin that served the sitemap.

    Static-site generators emit their build host — ``localhost:4321`` and
    friends — into sitemaps. Dropping those loses real pages, so they are
    re-pointed at the public origin instead.
    """
    if not is_private_host(host_of(location)):
        return location
    parsed = urlparse(location)
    moved = urljoin(origin, parsed.path)
    return f"{moved}?{parsed.query}" if parsed.query else moved


class PendingSitemap(BaseModel):
    """One sitemap to walk, and how deep in the index it was found."""

    model_config = ConfigDict(frozen=True)

    url: str
    depth: int = 0


async def sitemap_locations(
    pages: PageCache, sitemap: str, *, max_depth: int = MAX_SITEMAP_DEPTH
) -> list[str]:
    """Every content URL reachable from ``sitemap``, following nested indexes.

    One unreadable sitemap inside an index is logged and skipped rather than
    losing every other branch with it.
    """
    parsed = urlparse(sitemap)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async def walked() -> AsyncIterator[str]:
        """Every location the walk reaches, in order and possibly repeated."""
        visited = dict[str, PendingSitemap]()
        pending = [PendingSitemap(url=sitemap)]
        while pending:
            current = pending.pop()
            if current.url in visited or current.depth > max_depth:
                continue
            visited[current.url] = current
            try:
                raw = await pages.read(current.url)
                page = sitemap_page(gunzipped(current.url, raw))
            except Exception as error:
                logger.warning(
                    "Sitemap unreadable, skipping: %s — %s: %s",
                    current.url,
                    type(error).__name__,
                    error,
                )
                logger.debug("Sitemap %s failed", current.url, exc_info=True)
                continue
            if page.is_index:
                pending.extend(
                    PendingSitemap(url=location, depth=current.depth + 1)
                    for location in page.locations
                )
                continue
            for location in page.locations:
                yield rehosted(location, origin)

    found = [location async for location in walked()]
    return list(dict.fromkeys(found))


async def robots_sitemaps(pages: PageCache, origin: str) -> list[str]:
    """The sitemaps ``robots.txt`` advertises, empty when it advertises none.

    Read with the standard library's robots parser rather than by hand: the
    file has a grammar, and one that already knows it will not mistake a
    comment or a stray colon for a declaration.
    """
    try:
        text = await pages.read_text(f"{origin}/robots.txt")
    except Exception:
        logger.debug("No robots.txt at %s", origin, exc_info=True)
        return []
    rules = robotparser.RobotFileParser()
    rules.parse(text.splitlines())
    return list(rules.site_maps() or [])


# ── Reading HTML structure ────────────────────────────────────────────────────


class Attribute(BaseModel):
    """One HTML attribute, named rather than positional.

    ``html.parser`` hands attributes over as bare pairs, which say nothing
    about which side is which; naming them here is what lets the rest of this
    module read them without unpacking by index.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: str


AttributePair = Sequence[str | None]
"""How ``html.parser`` reports one attribute: a name beside an optional value."""


def named_attribute(pair: AttributePair) -> Attribute | None:
    """One attribute pair as a named attribute, or None where it is not a pair.

    A bare attribute such as ``disabled`` arrives with no value at all, which
    becomes the empty string so every caller reads the same type.
    """
    if len(pair) != 2:
        return None
    name, value = pair
    return None if name is None else Attribute(name=name, value=value or "")


def attribute_value(pairs: Sequence[AttributePair], name: str) -> str:
    """The value of one named attribute, empty when the tag carries none."""
    named = (named_attribute(pair) for pair in pairs)
    return next(
        (a.value for a in named if a is not None and a.name == name and a.value), ""
    )


class TableRow(BaseModel):
    """One table row, as the links it holds and the text of its cells."""

    model_config = ConfigDict(frozen=True)

    hrefs: tuple[str, ...] = ()
    cells: tuple[str, ...] = ()


class HtmlStructure(HTMLParser):
    """The links a page declares, and the table rows they sit in.

    A listing page names its items in ``href`` attributes and a listing table
    pairs each link with a human-readable label in its first cell. Both are
    structure, so both are read with a parser. Accumulating as the parse
    proceeds is what an event-driven parser is: these collections fill in
    tag-handler order, which no comprehension over the input could express.
    Nested tables are the one shape this flattens, which no listing here uses.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs = list[str]()
        self.rows = list[TableRow]()
        self.row_hrefs: list[str] | None = None
        self.row_cells: list[str] | None = None
        self.cell_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Sequence[AttributePair]) -> None:
        match tag:
            case "tr":
                self.row_hrefs = list[str]()
                self.row_cells = list[str]()
            case "td" | "th":
                self.cell_text = list[str]()
            case "a":
                href = attribute_value(attrs, "href")
                if href:
                    self.hrefs.append(href)
                    if self.row_hrefs is not None:
                        self.row_hrefs.append(href)
            case _:
                pass

    def handle_data(self, data: str) -> None:
        if self.cell_text is not None:
            self.cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        match tag:
            case "td" | "th":
                if self.cell_text is not None and self.row_cells is not None:
                    self.row_cells.append(" ".join("".join(self.cell_text).split()))
                self.cell_text = None
            case "tr":
                if self.row_hrefs is not None and self.row_cells is not None:
                    self.rows.append(
                        TableRow(
                            hrefs=tuple(self.row_hrefs), cells=tuple(self.row_cells)
                        )
                    )
                self.row_hrefs = None
                self.row_cells = None
            case _:
                pass


def html_structure(html: str) -> HtmlStructure:
    """The links and table rows of one HTML page."""
    parser = HtmlStructure()
    parser.feed(html)
    parser.close()
    return parser


def leaf_slug(path: str, prefix: str) -> str:
    """The single segment ``path`` adds after ``prefix``, empty when it adds none.

    This is what separates a document from the listing that names it: a leaf is
    the prefix plus exactly one more segment, so ``/research`` and
    ``/es/research/paper`` both fall away while ``/research/paper`` stays.
    """
    if not path.startswith(prefix):
        return ""
    remainder = path[len(prefix) :].removesuffix("/")
    return "" if not remainder or "/" in remainder else remainder


def url_slug(url: str, prefix: str) -> str:
    """``leaf_slug`` over a URL's decoded path."""
    return leaf_slug(unquote(urlparse(url).path), prefix)


# ── Avenues ───────────────────────────────────────────────────────────────────


class Avenue(BaseModel):
    """One publishing surface of one source, and how to walk it.

    Every avenue is a declaration: its fields say where to look and what
    counts as a document there, and ``discover`` is the walk that follows from
    them. A new source picks avenue types and fills in fields; it does not
    bring a new kind of walk.

    A declared union rather than an injected capability, so the base names the
    operation and each variant answers it — which is what keeps a new avenue
    type one class instead of an edit to every walk that would have to notice
    it. Abstract without naming ``ABC``: pydantic's own metaclass is an
    ``ABCMeta``, so the abstract methods below are enforced already.
    """

    model_config = ConfigDict(frozen=True)

    category: str = Field(
        default="",
        description="The section of the source these documents belong to",
    )
    tags: tuple[TagTerm, ...] = Field(
        default=(),
        description=(
            "What is true of everything published here, declared once rather "
            "than re-judged per document — a system-card listing publishes "
            "system cards, whatever each one is about"
        ),
    )

    @abstractmethod
    async def discover(self, pages: PageCache) -> list[DiscoveredItem]:
        """Enumerate the documents this avenue publishes."""

    @abstractmethod
    def origin(self) -> str:
        """The URL this avenue reads first, named in failure reports."""


class SitemapAvenue(Avenue):
    """A category whose documents a sitemap lists under one path prefix.

    The common shape by far: the sitemap names every page, and a document is
    one whose path is the prefix plus a single segment — which is what
    separates ``/research/some-paper`` from the bare ``/research`` listing and
    from localized mirrors under ``/es/research/...``.
    """

    sitemap: str = Field(description="Sitemap or sitemap-index URL to walk")
    path_prefix: str = Field(
        default="",
        description="Path the documents sit under; defaults to /<category>/",
    )

    def prefix(self) -> str:
        return self.path_prefix or f"/{self.category}/"

    def origin(self) -> str:
        return self.sitemap

    async def discover(self, pages: PageCache) -> list[DiscoveredItem]:
        """Every document this sitemap lists under the category's prefix.

        Both kinds of nothing are failures here, and they are different
        failures worth telling apart. A sitemap naming no location at all was
        not a sitemap — a moved one answers with the site's own page, which
        parses to nothing. A sitemap naming plenty and none of them under this
        prefix is a category that moved, and the message says which prefix
        found nothing so the declaration can be corrected rather than guessed at.
        """
        prefix = self.prefix()
        locations = await sitemap_locations(pages, self.sitemap)
        if not locations:
            raise AvenueEmpty(
                f"{self.sitemap} named no location — it has moved, or what it "
                "serves is no longer a sitemap"
            )
        found = [
            DiscoveredItem(category=self.category, slug=slug, url=url)
            for url in locations
            if (slug := url_slug(url, prefix))
        ]
        if not found:
            raise AvenueEmpty(
                f"{self.sitemap} named {len(locations)} location(s), none of "
                f"them under {prefix!r}"
            )
        return found


class ListingAvenue(Avenue):
    """A category whose documents only a paginated listing page names.

    Some sites publish no sitemap at all but render their listings
    server-side, so the items are in the initial HTML. Pagination is a
    per-list query parameter read off the page's own pagination link, and the
    walk stops as soon as a page adds nothing new.
    """

    listing: str = Field(description="URL of the listing page")
    item_prefix: str = Field(description="Path prefix marking a document link")
    page_parameter_suffix: str = Field(
        default="_page",
        description="Suffix of the query parameter that selects a listing page",
    )
    max_pages: int = Field(
        default=30, description="Cap on pages walked, against a pagination loop"
    )

    def origin(self) -> str:
        return self.listing

    def page_parameter(self, html: str) -> str:
        """The pagination parameter this listing uses, empty when unpaginated."""
        return next(
            (
                name
                for href in html_structure(html).hrefs
                for name in parse_qs(urlparse(href).query)
                if name.endswith(self.page_parameter_suffix)
            ),
            "",
        )

    def slugs_on(self, html: str) -> list[str]:
        """Slugs of every link on the page whose path is prefix plus a segment."""
        found = [
            slug
            for href in html_structure(html).hrefs
            if (slug := url_slug(href, self.item_prefix))
        ]
        return list(dict.fromkeys(found))

    def item_for(self, slug: str) -> DiscoveredItem:
        return DiscoveredItem(
            category=self.category,
            slug=slug,
            url=urljoin(self.listing, f"{self.item_prefix}{slug}"),
        )

    async def discover(self, pages: PageCache) -> list[DiscoveredItem]:
        found = dict[str, DiscoveredItem]()
        parameter = ""
        for page in range(1, self.max_pages + 1):
            if page == 1:
                url = self.listing
            elif parameter:
                url = f"{self.listing}?{parameter}={page}"
            else:
                break
            html = await pages.read_text(url)
            if page == 1:
                parameter = self.page_parameter(html)
            fresh = [slug for slug in self.slugs_on(html) if slug not in found]
            if not fresh:
                break
            for slug in fresh:
                found[slug] = self.item_for(slug)
        return [found[slug] for slug in sorted(found)]


class TableAvenue(Avenue):
    """A category listed only in a table, whose first cell carries the title.

    The shape that forces this is a table of hash-named PDFs: nothing in the
    URL says which model a card describes, so the listing's own label is the
    only readable title and discovery has to capture it.
    """

    listing: str = Field(description="URL of the page holding the table")
    link_hosts: tuple[str, ...] = Field(
        default=(), description="Hosts a document link may point at (any, if empty)"
    )
    link_suffixes: tuple[str, ...] = Field(
        default=(), description="Path suffixes marking a document link"
    )
    link_contains: tuple[str, ...] = Field(
        default=(), description="Substrings marking a document link"
    )
    strip_suffix: bool = Field(
        default=True, description="Drop the file suffix when deriving the slug"
    )

    def origin(self) -> str:
        return self.listing

    def is_document(self, href: str) -> bool:
        """Whether a row's link points at a document rather than elsewhere."""
        host = host_of(urljoin(self.listing, href))
        if self.link_hosts and not any(
            host == known or host.endswith(f".{known}") for known in self.link_hosts
        ):
            return False
        path = urlparse(href).path.lower()
        return any(path.endswith(s) for s in self.link_suffixes) or any(
            marker in path for marker in self.link_contains
        )

    def slug_for(self, href: str) -> str:
        """The slug of a document link: its last segment, suffix optionally cut."""
        name = PurePosixPath(urlparse(href).path.removesuffix("/")).name
        return slugified(PurePosixPath(name).stem if self.strip_suffix else name)

    async def discover(self, pages: PageCache) -> list[DiscoveredItem]:
        found = dict[str, DiscoveredItem]()
        for row in html_structure(await pages.read_text(self.listing)).rows:
            href = next((h for h in row.hrefs if self.is_document(h)), None)
            if href is None:
                continue
            slug = self.slug_for(href)
            if not slug or slug in found:
                continue
            found[slug] = DiscoveredItem(
                category=self.category,
                slug=slug,
                url=urljoin(self.listing, href),
                title=row.cells[0] if row.cells else "",
            )
        return list(found.values())


COMMON_EXCLUDE: tuple[str, ...] = (
    "/tag/",
    "/tags/",
    "/category/",
    "/categories/",
    "/author/",
    "/page/",
)
"""Taxonomy paths most content systems emit, which carry no document of their own."""

ASSET_SUFFIXES: tuple[str, ...] = (
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp4",
    ".webm",
    ".mov",
    ".mp3",
    ".wav",
    ".zip",
    ".gz",
    ".rss",
    ".atom",
)
"""Suffixes that are page furniture or machine feeds rather than documents.
Deliberately short: a sweep wants everything, so when in doubt it keeps. PDFs
are absent on purpose — a PDF is the document."""


class FeedEntry(BaseModel):
    """One entry of a syndication feed: where it points and what it is called."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str


def feed_entries(data: bytes) -> tuple[FeedEntry, ...]:
    """Read a syndication feed's entries, namespace- and dialect-agnostically.

    RSS puts the URL in ``<item><link>`` as text and Atom puts it in
    ``<entry><link href=...>``, so both are read rather than the feed being
    sniffed for which dialect it is. An entry with no resolvable URL is
    dropped: it names no document to fetch.
    """
    root = xml_root(data)

    def child_text(element: ElementTree.Element, name: str) -> str:
        """The first non-empty text of a named child, empty when there is none."""
        return next(
            (
                (child.text or "").strip()
                for child in element
                if local_name(child.tag) == name and (child.text or "").strip()
            ),
            "",
        )

    def entry_url(element: ElementTree.Element) -> str:
        """The document an entry points at, by whichever dialect carries it."""
        for child in element:
            if local_name(child.tag) != "link":
                continue
            href = (
                child.attrib["href"] if "href" in child.attrib else (child.text or "")
            )
            if href.strip():
                return href.strip()
        return ""

    return tuple(
        FeedEntry(url=url, title=child_text(element, "title"))
        for element in root.iter()
        if local_name(element.tag) in ("item", "entry")
        if (url := entry_url(element))
    )


class FeedAvenue(Avenue):
    """A topic feed a source publishes, walked as the source's own filter.

    News outlets are the case this exists for. A sitemap or a whole-domain
    sweep of an outlet enumerates everything it has ever published, and the
    corpus wants one subject out of that. The outlet already maintains that
    selection — its own AI section, as a feed — so the filter is the
    publisher's editorial judgement applied at enumeration, and nothing off
    the topic is ever fetched.

    A feed is a window rather than an archive: it names what is recent, not
    what exists. Incremental syncs are what accumulate the back catalogue,
    which is why the corpus keys identity off the URL — the same article seen
    on two runs is one document.
    """

    feed: str = Field(description="URL of the RSS or Atom feed to walk")
    apex: str = Field(
        default="", description="Registrable domain for cross-subdomain slugs"
    )
    require_terms: tuple[str, ...] = Field(
        default=(),
        description=(
            "Keep only entries whose title contains one of these, matched "
            "case-insensitively. Empty keeps everything, which is right when "
            "the feed is already the topic — an outlet's own AI section needs "
            "no second filter, and its security section does"
        ),
    )

    def origin(self) -> str:
        return self.feed

    def resolved_apex(self) -> str:
        """The apex a declaration states, else the feed's own host."""
        return self.apex or host_of(self.feed).removeprefix("www.")

    def on_topic(self, title: str) -> bool:
        """Whether an entry's title carries one of the required terms.

        Titles only, which is the honest limit of filtering before fetching:
        an article about a model that never names one in its headline is
        missed. The alternative is fetching an outlet's whole output to judge
        it, which is what declaring a topic feed was meant to avoid.
        """
        if not self.require_terms:
            return True
        lowered = title.lower()
        return any(term.lower() in lowered for term in self.require_terms)

    async def discover(self, pages: PageCache) -> list[DiscoveredItem]:
        apex = self.resolved_apex()
        found = dict[str, DiscoveredItem]()
        for entry in feed_entries(await pages.read(self.feed)):
            slug = source_id_for(entry.url, apex)
            if slug not in found and self.on_topic(entry.title):
                found[slug] = DiscoveredItem(
                    category=self.category,
                    slug=slug,
                    url=entry.url,
                    title=entry.title,
                )
        return [found[slug] for slug in sorted(found)]


class SweepAvenue(Avenue):
    """Everything a source publishes across every domain it publishes on.

    Where a per-category avenue asks for one section, a sweep takes the whole
    surface: each domain's sitemaps, minus structural noise, plus seeds for
    documents no sitemap lists — CDN-hosted PDFs and JS-only pages. The
    category of an item is its first path segment, since a whole-site sweep
    has no single section to name.
    """

    domains: tuple[str, ...] = Field(
        default=(), description="Every origin whose sitemaps this sweep walks"
    )
    seeds: tuple[str, ...] = Field(
        default=(), description="URLs no sitemap lists, always kept"
    )
    exclude: tuple[str, ...] = Field(
        default=COMMON_EXCLUDE, description="Path prefixes to drop"
    )
    apex: str = Field(
        default="", description="Registrable domain for cross-subdomain slugs"
    )
    root_category: str = Field(
        default="root", description="Category for a document at the domain root"
    )

    def origin(self) -> str:
        if self.domains:
            return self.domains[0]
        return self.seeds[0] if self.seeds else ""

    def resolved_apex(self) -> str:
        """The apex a declaration states, else the primary domain's own host."""
        return self.apex or host_of(self.origin()).removeprefix("www.")

    def keep(self, url: str) -> bool:
        """Whether a swept URL is a document; seeds never come through here."""
        path = unquote(urlparse(url).path)
        if PurePosixPath(path).suffix.lower() in ASSET_SUFFIXES:
            return False
        return not any(path.startswith(prefix) for prefix in self.exclude)

    def category_for(self, url: str) -> str:
        segments = path_segments(url)
        return segments[0] if len(segments) > 1 else self.root_category

    def item_for(self, url: str, apex: str) -> DiscoveredItem:
        return DiscoveredItem(
            category=self.category or self.category_for(url),
            slug=source_id_for(url, apex),
            url=url,
        )

    async def swept_urls(self, pages: PageCache) -> list[str]:
        """Every URL the declared domains' sitemaps list."""

        async def listed() -> AsyncIterator[str]:
            for domain in self.domains:
                parsed = urlparse(domain if "://" in domain else f"https://{domain}")
                origin = f"{parsed.scheme}://{parsed.netloc}"
                advertised = await robots_sitemaps(pages, origin) or [
                    f"{origin}/sitemap.xml"
                ]
                for sitemap in advertised:
                    for url in await sitemap_locations(pages, sitemap):
                        yield url

        return [url async for url in listed()]

    async def discover(self, pages: PageCache) -> list[DiscoveredItem]:
        apex = self.resolved_apex()
        found = dict[str, DiscoveredItem]()
        for url in await self.swept_urls(pages):
            key = normalize_url(url)
            if key in found or not path_segments(url) or not self.keep(url):
                continue
            found[key] = self.item_for(url, apex)
        for url in self.seeds:
            key = normalize_url(url)
            if key not in found:
                found[key] = self.item_for(url, apex)
        if not found:
            raise AvenueEmpty(
                f"none of {', '.join(self.domains)} named a document — every "
                "sitemap was empty, unreadable, or filtered away entirely"
            )
        return list(found.values())


# ── Running discovery for a source ────────────────────────────────────────────


class AvenueEmpty(Exception):
    """An avenue that was reached and turned out to enumerate nothing.

    Its own failure because it is the one a run reports as success. A source
    that is down fails loudly and lands in the failures; a source that *moved*
    answers every request and names no document, so the run says "0 listed, 0
    failed" — which reads as a source that publishes nothing rather than as a
    declaration pointing somewhere that is no longer there.

    Apollo is the case this was written for: it left WordPress for Webflow, the
    per-type sitemaps it used to publish became ordinary web pages, and those
    parse to no locations at all. It sat broken through a whole sweep looking
    exactly like a quiet source.

    Raised by the avenue rather than by the sitemap walk, because whether zero
    is a failure is the avenue's question: one sitemap naming nothing is a dead
    declaration, while one domain of a multi-domain sweep naming nothing is
    ordinary and the sweep carries on to the others.
    """


class AvenueFailure(BaseModel):
    """One avenue that could not be enumerated, and why."""

    model_config = ConfigDict(frozen=True)

    category: str
    origin: str
    error: str


class AvenueOutcome(BaseModel):
    """What walking one avenue produced: its items, or the failure instead."""

    model_config = ConfigDict(frozen=True)

    items: tuple[DiscoveredItem, ...] = ()
    failure: AvenueFailure | None = None


async def walk_avenue(avenue: Avenue, pages: PageCache) -> AvenueOutcome:
    """Enumerate one avenue, turning a failure into a recorded outcome.

    Avenues fail independently — a listing page can be down while the sitemap
    is fine — so a failure here becomes data the caller keeps beside the other
    avenues' results, never an exception that discards them.
    """
    try:
        found = await avenue.discover(pages)
    except Exception as exc:
        logger.warning(
            "Avenue %s at %s failed", avenue.category, avenue.origin(), exc_info=True
        )
        return AvenueOutcome(
            failure=AvenueFailure(
                category=avenue.category,
                origin=avenue.origin(),
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    return AvenueOutcome(items=tuple(found))


class DiscoveryOutcome(BaseModel):
    """What one source's enumeration found, and what it could not reach."""

    model_config = ConfigDict(frozen=True)

    items: tuple[DiscoveredItem, ...] = ()
    failures: tuple[AvenueFailure, ...] = ()

    def reached_nothing(self) -> bool:
        """Whether every avenue failed, so an empty result proves nothing."""
        return not self.items and bool(self.failures)


async def discover_avenues(
    avenues: tuple[Avenue, ...], pages: PageCache
) -> DiscoveryOutcome:
    """Enumerate every avenue of one source, keeping what succeeds.

    Slugs are unique per source, so an item an earlier avenue already named
    wins: Anthropic's research sitemap and its system-card table can point at
    the same page, and the corpus holds one document either way.
    """
    outcomes = [await walk_avenue(avenue, pages) for avenue in avenues]
    merged = dict[str, DiscoveredItem]()
    for outcome in outcomes:
        for item in outcome.items:
            if item.slug not in merged:
                merged[item.slug] = item
    return DiscoveryOutcome(
        items=tuple(merged.values()),
        failures=tuple(o.failure for o in outcomes if o.failure is not None),
    )
