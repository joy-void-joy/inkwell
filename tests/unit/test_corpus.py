"""The corpus: declarations, incremental enumeration, storage, and resilience.

What is worth testing here is the behaviour the design promises and a later
change could quietly break — that a re-run adds only what is new, that the index
round-trips, that a PDF is never turned into text, and that one dead source
costs only itself. The network is faked with a fixture reader and an httpx mock
transport, so the real extraction, hashing, and merge paths all run.
"""

import json
from pathlib import Path
from typing import get_args

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from inkwell.agent.provenance import Venue
from inkwell.corpus.discovery import (
    Avenue,
    DiscoveredItem,
    DiscoveryOutcome,
    FeedAvenue,
    ListingAvenue,
    PageCache,
    PageReader,
    SitemapAvenue,
    SweepAvenue,
    TableAvenue,
    discover_avenues,
    feed_entries,
    source_id_for,
)
from inkwell.corpus.fetch import DocumentFetcher, HttpPageReader
from inkwell.corpus.ingest import ingest_source, ingest_with
from inkwell.corpus.quality import DEFAULT_RULES, ThinRule, assess
from inkwell.corpus.registry import (
    DECLARED_SOURCES,
    DROPPED_SOURCES,
    SourceDeclaration,
    active_declarations,
    corpus_vocabulary,
    declaration_for,
)
from inkwell.corpus.storage import (
    CorpusStore,
    SourceShard,
    StoredDocument,
    merged_discovery,
    pending_entries,
)
from inkwell.corpus.tagging import (
    DocumentTagger,
    TagJudge,
    TagJudgement,
    TagRequest,
    retag_source,
)
from inkwell.corpus.tags import (
    DEFAULT_SUBJECTS,
    DEFAULT_VOCABULARY,
    EVALUATIONS,
    GOVERNANCE,
    SYSTEM_CARD,
    DocumentTags,
    TagTerm,
    TagVocabulary,
    folded,
)

FIXTURE_HOST = "https://fixture.test"
SITEMAP = f"{FIXTURE_HOST}/sitemap.xml"

PDF_BYTES_SAMPLE = b"%PDF-1.4\ntrailer<</Root 1 0 R>>\n"
"""Enough of a PDF to be recognised as one, for the refusal check."""


def one_page_pdf() -> bytes:
    """A real one-page PDF, so reading its page count exercises the real path.

    Hand-built rather than produced by a library: nothing in this project
    writes PDFs, so a fixture that needed one would be a dependency carried
    for a test alone.
    """
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj".encode() + payload + b"endobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (
        f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(body)


class FixturePage(BaseModel):
    """One canned response: what a URL serves and what it says it is."""

    model_config = ConfigDict(frozen=True)

    url: str
    body: bytes
    content_type: str = "text/html; charset=utf-8"


class FixtureReader(PageReader):
    """Serves canned pages; anything unnamed raises, as a dead URL would."""

    def __init__(self, pages: tuple[FixturePage, ...]) -> None:
        self.pages = {page.url: page for page in pages}
        self.reads = list[str]()

    async def read(self, url: str) -> bytes:
        self.reads.append(url)
        if url not in self.pages:
            raise httpx.ConnectError(f"no fixture for {url}")
        return self.pages[url].body


def sitemap_xml(paths: tuple[str, ...]) -> bytes:
    """A sitemap urlset listing ``paths`` under the fixture host."""
    locations = "".join(f"<url><loc>{FIXTURE_HOST}{path}</loc></url>" for path in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locations}</urlset>"
    ).encode()


ARTICLE_SENTENCE = (
    "Evaluations of frontier systems depend on the harness as much as the model, "
    "and a result that omits the harness is not reproducible by anyone else. "
)


def article_html(title: str) -> bytes:
    """A page with enough structure and length for the real extraction to keep it."""
    paragraphs = "".join(
        f"<p>{ARTICLE_SENTENCE} This is paragraph {n} of the discussion.</p>"
        for n in range(1, 13)
    )
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<nav>Home About Careers</nav>"
        f"<article><h1>{title}</h1>{paragraphs}</article>"
        f"<footer>Copyright</footer></body></html>"
    ).encode()


def fixture_declaration(
    *,
    key: str = "fixture",
    display_name: str = "Fixture Source",
    avenues: tuple[Avenue, ...] = (),
) -> SourceDeclaration:
    """A declaration pointed at the fixture host's sitemap."""
    return SourceDeclaration(
        key=key,
        display_name=display_name,
        organization="Fixture Org",
        venue="Fixture Venue",
        authority="lab_publication",
        hosts=("fixture.test",),
        avenues=avenues or (SitemapAvenue(category="research", sitemap=SITEMAP),),
    )


class FixtureJudge(TagJudge):
    """A judgement that answers from a canned map and records what it was asked.

    Recording the requests is what lets a test check the *shape* of the
    question — that a judgement is never asked to settle what the registry
    already declares, and that it is handed a body it can actually read.
    """

    def __init__(
        self,
        tags: dict[str, tuple[str, ...]] | None = None,
        *,
        default: tuple[str, ...] = (),
        summary: str = "",
        answers: bool = True,
    ) -> None:
        self.tags = tags or {}
        self.default = default
        self.summary = summary
        self.answers = answers
        self.requests = list[TagRequest]()

    async def judge(self, request: TagRequest) -> TagJudgement | None:
        self.requests.append(request)
        if not self.answers:
            return None
        canned = self.tags[request.slug] if request.slug in self.tags else self.default
        return TagJudgement(
            tags=canned, summary=self.summary or f"What {request.slug} says."
        )


def fixture_tagger(
    judge: TagJudge | None = None, vocabulary: TagVocabulary | None = None
) -> DocumentTagger:
    """A tagger that spends no model, for the ingestion paths under test."""
    return DocumentTagger(
        vocabulary=vocabulary if vocabulary is not None else corpus_vocabulary(),
        judge=judge if judge is not None else FixtureJudge(),
    )


def outcome_with(slugs: tuple[str, ...]) -> DiscoveryOutcome:
    """A discovery outcome naming ``slugs`` under the fixture host."""
    return DiscoveryOutcome(
        items=tuple(
            DiscoveredItem(
                category="research", slug=slug, url=f"{FIXTURE_HOST}/research/{slug}"
            )
            for slug in slugs
        )
    )


def transport_for(pages: tuple[FixturePage, ...]) -> httpx.MockTransport:
    """A transport serving the fixture pages, 404 for anything else."""
    index = {page.url: page for page in pages}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in index:
            return httpx.Response(404, text="not found")
        page = index[url]
        return httpx.Response(
            200, content=page.body, headers={"content-type": page.content_type}
        )

    return httpx.MockTransport(handler)


def client_for(pages: tuple[FixturePage, ...]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport_for(pages))


async def enumerate_with(
    declaration: SourceDeclaration, pages: tuple[FixturePage, ...]
) -> DiscoveryOutcome:
    """Run a declaration's avenues against canned pages."""
    return await discover_avenues(
        declaration.avenues, PageCache(reader=FixtureReader(pages))
    )


# ── The registry is a declaration, and accounts for every ported source ───────


def test_every_declared_source_is_named_once() -> None:
    keys = [entry.key for entry in DECLARED_SOURCES]
    assert keys
    assert len(keys) == len(dict.fromkeys(keys))


def test_only_sources_whose_enumeration_is_proven_are_active() -> None:
    """The seven the ported database enumerated, plus the news desks whose
    feeds were walked for real, plus the two whose first sweep was taken
    deliberately: OpenAI, whose surface is large, and BleepingComputer, which
    refuses a plain client and is reached over the browser's connection."""
    assert {entry.key for entry in active_declarations()} == {
        "aisi",
        "anthropic",
        "apollo",
        "epoch",
        "govai",
        "iaps",
        "metr",
        "techcrunch",
        "cyberscoop",
        "bbc",
        "openai",
        "bleepingcomputer",
    }


def test_an_inactive_source_still_declares_where_and_why() -> None:
    """Declaring and sweeping are separate: an inactive source is still data."""
    inactive = [entry for entry in DECLARED_SOURCES if not entry.active]
    assert inactive
    for entry in inactive:
        assert entry.hosts, f"{entry.key} declares no host"
        assert entry.avenues, f"{entry.key} declares no avenue"
        assert entry.notes, f"{entry.key} does not say why it is inactive"


def test_every_declaration_carries_venue_and_authority() -> None:
    """Authority is derived from the declaration, never asserted per citation.

    Checked against the venue vocabulary itself rather than a list repeated
    here, because a second list is the thing this collapse removed: one that
    fell behind would pass a declaration the derivation cannot read.
    """
    for entry in DECLARED_SOURCES:
        assert entry.venue
        assert entry.organization
        assert entry.authority in get_args(Venue.__value__)


def test_dropped_sources_are_recorded_with_a_reason() -> None:
    """What the ported database handled but this corpus omits, and why."""
    assert {entry.key for entry in DROPPED_SOURCES} == {
        "arxiv",
        "wikipedia",
        "substack",
        "alignment_forum",
        "pdf",
    }
    for entry in DROPPED_SOURCES:
        assert entry.reason
        assert declaration_for(entry.key) is None


def test_a_source_needing_a_browser_says_so() -> None:
    """The playwright path is chosen by declaration, not guessed per page."""
    browser_bound = [entry for entry in DECLARED_SOURCES if entry.needs_browser]
    assert {entry.key for entry in browser_bound} == {
        "deepmind",
        "xai",
        "bleepingcomputer",
    }


def declared_union_bases() -> list[type[BaseModel]]:
    """The bases that name an operation for their variants to answer.

    Returned as the base type so the check below is about being abstract, not
    about either class's own fields.
    """
    from inkwell.corpus.quality import QualityRule

    return [Avenue, QualityRule]


def test_the_declared_unions_stay_abstract() -> None:
    """Pydantic's metaclass enforces this, so neither base needs to name ABC."""
    for base in declared_union_bases():
        with pytest.raises(TypeError, match="abstract"):
            base()


def test_declaring_a_source_needs_no_new_module() -> None:
    """Every avenue in the registry is one of the declared avenue types."""
    kinds = (SitemapAvenue, ListingAvenue, TableAvenue, SweepAvenue, FeedAvenue)
    for entry in DECLARED_SOURCES:
        for avenue in entry.avenues:
            assert isinstance(avenue, kinds), f"{entry.key} brought its own avenue"


# ── Enumeration is incremental, keyed on identity ─────────────────────────────


async def test_discovery_finds_the_leaf_documents_only() -> None:
    outcome = await enumerate_with(
        fixture_declaration(),
        (
            FixturePage(
                url=SITEMAP,
                body=sitemap_xml(
                    ("/research/first", "/research/second", "/research", "/about")
                ),
            ),
        ),
    )
    assert [item.slug for item in outcome.items] == ["first", "second"]
    assert not outcome.failures


async def test_a_rerun_adds_only_what_is_new_and_keeps_first_seen() -> None:
    """Incremental means keyed on identity: first_seen survives every later run."""
    declaration = fixture_declaration()
    shard = SourceShard(source="fixture")

    first = await enumerate_with(
        declaration,
        (
            FixturePage(
                url=SITEMAP, body=sitemap_xml(("/research/one", "/research/two"))
            ),
        ),
    )
    merged_discovery(shard, first, now="2026-01-01T00:00:00Z")
    assert [entry.slug for entry in shard.discovered] == ["one", "two"]

    second = await enumerate_with(
        declaration,
        (
            FixturePage(
                url=SITEMAP,
                body=sitemap_xml(("/research/one", "/research/two", "/research/three")),
            ),
        ),
    )
    merged_discovery(shard, second, now="2026-02-02T00:00:00Z")

    by_slug = shard.discovered_by_slug()
    assert by_slug.keys() == {"one", "two", "three"}
    assert by_slug["one"].first_seen == "2026-01-01T00:00:00Z"
    assert by_slug["one"].last_seen == "2026-02-02T00:00:00Z"
    assert by_slug["three"].first_seen == "2026-02-02T00:00:00Z"


async def test_a_document_that_stops_being_listed_is_kept_and_marked_absent() -> None:
    declaration = fixture_declaration()
    shard = SourceShard(source="fixture")
    for paths in (("/research/one", "/research/two"), ("/research/one",)):
        outcome = await enumerate_with(
            declaration, (FixturePage(url=SITEMAP, body=sitemap_xml(paths)),)
        )
        merged_discovery(shard, outcome)
    by_slug = shard.discovered_by_slug()
    assert by_slug["one"].present
    assert not by_slug["two"].present


async def test_an_empty_enumeration_does_not_erase_a_populated_source() -> None:
    """A crawl that suddenly finds nothing is a failure, not a mass deletion."""
    declaration = fixture_declaration()
    shard = SourceShard(source="fixture")
    populated = sitemap_xml(tuple(f"/research/doc-{n}" for n in range(12)))
    merged_discovery(
        shard,
        await enumerate_with(declaration, (FixturePage(url=SITEMAP, body=populated),)),
    )
    assert len(shard.discovered) == 12

    merged_discovery(
        shard,
        await enumerate_with(
            declaration, (FixturePage(url=SITEMAP, body=sitemap_xml(())),)
        ),
    )
    assert shard.crawl_degraded
    assert sum(1 for entry in shard.discovered if entry.present) == 12


async def test_one_sitemap_is_read_once_however_many_avenues_want_it() -> None:
    reader = FixtureReader(
        (FixturePage(url=SITEMAP, body=sitemap_xml(("/research/a", "/blog/b"))),)
    )
    declaration = fixture_declaration(
        avenues=(
            SitemapAvenue(category="research", sitemap=SITEMAP),
            SitemapAvenue(category="blog", sitemap=SITEMAP),
        )
    )
    outcome = await discover_avenues(declaration.avenues, PageCache(reader=reader))
    assert {item.slug for item in outcome.items} == {"a", "b"}
    assert reader.reads.count(SITEMAP) == 1


def test_pending_skips_what_is_already_stored() -> None:
    shard = SourceShard(source="fixture")
    merged_discovery(shard, outcome_with(("kept", "missing")))
    shard.record_document(
        StoredDocument(
            slug="kept",
            url=f"{FIXTURE_HOST}/research/kept",
            content_sha256="abc",
            filename="kept.md",
        )
    )
    assert [entry.slug for entry in pending_entries(shard)] == ["missing"]
    assert {entry.slug for entry in pending_entries(shard, refresh=True)} == {
        "kept",
        "missing",
    }


def test_source_id_is_derived_from_the_url_alone() -> None:
    """The same URL always yields the same identity, across subdomains."""
    assert (
        source_id_for("https://epoch.ai/publications/a-paper") == "publications-a-paper"
    )
    assert (
        source_id_for("https://deploymentsafety.openai.com/gpt-5", "openai.com")
        == "deploymentsafety__gpt-5"
    )
    assert (
        source_id_for("https://www.openai.com/index/foo", "openai.com") == "index-foo"
    )


async def test_a_sitemap_declaring_a_dtd_is_refused() -> None:
    """Third-party XML gets no chance to declare entities."""
    hostile = b'<?xml version="1.0"?><!DOCTYPE urlset [<!ENTITY a "b">]><urlset/>'
    outcome = await enumerate_with(
        fixture_declaration(), (FixturePage(url=SITEMAP, body=hostile),)
    )
    assert outcome.items == ()


# ── The index round-trips, and the layout is the interface ────────────────────


def test_the_shard_round_trips_through_its_json(tmp_path: Path) -> None:
    store = CorpusStore(root=tmp_path)
    declaration = fixture_declaration()
    shard = store.load(declaration)
    merged_discovery(shard, outcome_with(("one",)))
    shard.record_document(
        StoredDocument(
            slug="one",
            url=f"{FIXTURE_HOST}/research/one",
            category="research",
            title="One",
            filename="one.md",
            content_sha256="deadbeef",
            quality=assess("body text " * 200, title="One"),
        )
    )
    store.save(shard)

    reloaded = store.load(declaration)
    assert reloaded.model_dump() == shard.model_dump()
    assert reloaded.stored("one") is not None


def test_the_shard_is_one_file_per_source_beside_the_documents(tmp_path: Path) -> None:
    store = CorpusStore(root=tmp_path)
    store.save(SourceShard(source="alpha"))
    store.save(SourceShard(source="beta"))
    store.store_markdown("alpha", "doc", "# Doc\n\nBody.\n")

    assert (tmp_path / "alpha" / "index.json").is_file()
    assert (tmp_path / "beta" / "index.json").is_file()
    written = (tmp_path / "alpha" / "doc.md").read_text(encoding="utf-8")
    assert written.startswith("# Doc")
    assert store.sources() == ("alpha", "beta")


def test_an_unreadable_index_does_not_stop_a_run(tmp_path: Path) -> None:
    store = CorpusStore(root=tmp_path)
    declaration = fixture_declaration()
    path = store.index_path(declaration.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert store.load(declaration).documents == []


def test_a_long_slug_still_yields_a_writable_filename(tmp_path: Path) -> None:
    store = CorpusStore(root=tmp_path)
    filename = store.store_markdown("alpha", "a" * 400, "body")
    assert (tmp_path / "alpha" / filename).is_file()
    assert len(filename) < 255


# ── A PDF stays a PDF ─────────────────────────────────────────────────────────


def test_a_pdf_is_stored_byte_for_byte(tmp_path: Path) -> None:
    store = CorpusStore(root=tmp_path)
    original = one_page_pdf()
    filename = store.store_pdf("alpha", "paper", original)
    assert filename.endswith(".pdf")
    assert (tmp_path / "alpha" / filename).read_bytes() == original


def test_storing_pdf_bytes_as_markdown_is_refused(tmp_path: Path) -> None:
    """The no-extraction rule holds by construction, not by remembering it."""
    store = CorpusStore(root=tmp_path)
    with pytest.raises(ValueError, match="store_pdf"):
        store.store_markdown("alpha", "paper", PDF_BYTES_SAMPLE.decode("latin-1"))


async def test_ingesting_a_pdf_never_extracts_its_text(tmp_path: Path) -> None:
    """A PDF arrives, is stored verbatim, and the index locates it instead."""
    declaration = fixture_declaration()
    original = one_page_pdf()
    pages = (
        FixturePage(url=SITEMAP, body=sitemap_xml(("/research/paper",))),
        FixturePage(
            url=f"{FIXTURE_HOST}/research/paper",
            body=original,
            content_type="application/pdf",
        ),
    )
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        report = await ingest_source(
            declaration, store, DocumentFetcher(client=client), tagger=fixture_tagger()
        )

    assert report.stored == 1
    document = store.load(declaration).stored("paper")
    assert document is not None
    assert document.kind == "pdf"
    assert document.filename.endswith(".pdf")
    assert document.abstract == ""
    assert not document.quality.assessed
    assert document.page_count == 1
    assert document.url.endswith("/research/paper")

    directory = tmp_path / declaration.key
    assert (directory / document.filename).read_bytes() == original
    assert list(directory.glob("*.md")) == []


# ── Quality is a declared rule set with overridable thresholds ────────────────


def test_a_score_names_the_rules_it_came_from() -> None:
    report = assess("short", title="")
    assert "thin" in report.fired
    assert "no_title" in report.fired
    assert not report.ok
    assert report.score < 1.0


def test_a_threshold_is_overridden_by_declaring_the_rule_differently() -> None:
    lenient = tuple(
        ThinRule(min_chars=5) if rule.rule_id == "thin" else rule
        for rule in DEFAULT_RULES
    )
    body = "a short note that a notes publisher would call complete"
    assert "thin" in assess(body, title="Note").fired
    assert "thin" not in assess(body, title="Note", rules=lenient).fired


def test_quality_is_recorded_per_document(tmp_path: Path) -> None:
    store = CorpusStore(root=tmp_path)
    declaration = fixture_declaration()
    shard = store.load(declaration)
    shard.record_document(
        StoredDocument(slug="one", url="u", quality=assess("tiny", title=""))
    )
    store.save(shard)
    document = store.load(declaration).stored("one")
    assert document is not None
    assert document.quality.fired
    assert document.quality.metrics.chars == 4


# ── A host that refuses a plain client is reached through the browser ─────────


def refusing_transport(status: int = 403) -> httpx.MockTransport:
    """A transport that turns every request away, as an anti-bot host does."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="forbidden")

    return httpx.MockTransport(handler)


async def test_a_refused_document_is_reached_over_the_browsers_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 raises before any body exists, so the escalation cannot key on one.

    This is the whole of what the thin-body path could not do: a refusal never
    produces the short extraction that path is watching for, so a source that
    answers a browser and refuses a client would declare the browser path and
    never once take it.
    """
    reached: list[str] = []

    async def through_browser(url: str, *, profile: str | None = None) -> bytes:
        reached.append(url)
        return article_html("Reached")

    monkeypatch.setattr("inkwell.corpus.fetch.fetched_through_browser", through_browser)
    declaration = fixture_declaration()
    browser_bound = declaration.model_copy(update={"needs_browser": True})
    url = f"{FIXTURE_HOST}/research/one"
    async with httpx.AsyncClient(transport=refusing_transport()) as client:
        document = await DocumentFetcher(client=client).fetch(url, browser_bound)

    assert reached == [url]
    assert document.kind == "markdown"
    assert document.rendered
    assert "Reached" in document.text


async def test_a_refusal_is_let_through_where_no_browser_is_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escalation is what a source declared, never what a status code suggests.

    Otherwise every genuinely dead URL in a sweep would cost a browser launch.
    """

    async def through_browser(url: str, *, profile: str | None = None) -> bytes:
        raise AssertionError(f"a browser was launched for undeclared {url}")

    monkeypatch.setattr("inkwell.corpus.fetch.fetched_through_browser", through_browser)
    declaration = fixture_declaration()
    async with httpx.AsyncClient(transport=refusing_transport()) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await DocumentFetcher(client=client).fetch(
                f"{FIXTURE_HOST}/research/one", declaration
            )


async def test_a_refused_feed_is_reached_too_so_the_source_enumerates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source refused at its feed discovers nothing to fetch in the first place.

    Bytes rather than a rendered DOM: a feed put through a browser's XML viewer
    comes back as that viewer's HTML, which the entry parser cannot read.
    """
    feed = "https://fixture.test/feed/"
    served = (
        b'<?xml version="1.0"?>\n<rss version="2.0"><channel>'
        b"<title>Fixture Desk</title><item>"
        b"<title>One</title>"
        b"<link>https://fixture.test/research/one/</link>"
        b"</item></channel></rss>"
    )

    async def through_browser(url: str, *, profile: str | None = None) -> bytes:
        return served

    monkeypatch.setattr("inkwell.corpus.fetch.fetched_through_browser", through_browser)
    avenue = FeedAvenue(category="security", feed=feed, apex="fixture.test")
    declaration = fixture_declaration(avenues=(avenue,)).model_copy(
        update={"needs_browser": True}
    )
    async with httpx.AsyncClient(transport=refusing_transport()) as client:
        found = await avenue.discover(
            PageCache(reader=HttpPageReader(client, declaration))
        )

    assert [item.url for item in found] == ["https://fixture.test/research/one/"]


# ── Runs are observable, and one source failing costs only itself ─────────────


async def test_a_run_reports_what_it_discovered_fetched_and_skipped(
    tmp_path: Path,
) -> None:
    declaration = fixture_declaration()
    pages = (
        FixturePage(url=SITEMAP, body=sitemap_xml(("/research/one", "/research/two"))),
        FixturePage(url=f"{FIXTURE_HOST}/research/one", body=article_html("One")),
        FixturePage(url=f"{FIXTURE_HOST}/research/two", body=article_html("Two")),
    )
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        first = await ingest_source(
            declaration,
            store,
            DocumentFetcher(client=client),
            tagger=fixture_tagger(),
            limit=1,
        )
        assert first.discovered == 2
        assert first.new == 2
        assert first.stored == 1
        assert first.skipped == 1

        second = await ingest_source(
            declaration, store, DocumentFetcher(client=client), tagger=fixture_tagger()
        )
    assert second.new == 0
    assert second.stored == 1
    assert second.skipped == 0
    assert "discovered 2" in second.summary()


async def test_a_stored_document_is_not_rewritten_when_it_has_not_changed(
    tmp_path: Path,
) -> None:
    """Identity is the content hash, so a refresh over static pages is a no-op."""
    declaration = fixture_declaration()
    pages = (
        FixturePage(url=SITEMAP, body=sitemap_xml(("/research/one",))),
        FixturePage(url=f"{FIXTURE_HOST}/research/one", body=article_html("One")),
    )
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        await ingest_source(
            declaration, store, DocumentFetcher(client=client), tagger=fixture_tagger()
        )
        again = await ingest_source(
            declaration,
            store,
            DocumentFetcher(client=client),
            tagger=fixture_tagger(),
            refresh=True,
        )
    assert again.stored == 0
    assert again.unchanged == 1


async def test_a_document_failure_is_recorded_for_the_next_run(tmp_path: Path) -> None:
    declaration = fixture_declaration()
    pages = (
        FixturePage(
            url=SITEMAP, body=sitemap_xml(("/research/here", "/research/gone"))
        ),
        FixturePage(url=f"{FIXTURE_HOST}/research/here", body=article_html("Here")),
    )
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        report = await ingest_source(
            declaration, store, DocumentFetcher(client=client), tagger=fixture_tagger()
        )

    assert report.stored == 1
    assert report.failed == 1
    shard = store.load(declaration)
    failures = shard.failures_by_slug()
    assert failures["gone"].failure_class == "upstream_missing"
    assert failures["gone"].attempts == 1
    assert [entry.slug for entry in pending_entries(shard)] == ["gone"]


async def test_one_unreachable_source_does_not_take_the_run_down(
    tmp_path: Path,
) -> None:
    """The other sources still sync, and the dead one's failure is recorded."""
    healthy = fixture_declaration()
    broken = fixture_declaration(
        key="broken",
        display_name="Broken Source",
        avenues=(
            SitemapAvenue(
                category="research", sitemap="https://broken.test/sitemap.xml"
            ),
        ),
    )
    pages = (
        FixturePage(url=SITEMAP, body=sitemap_xml(("/research/one",))),
        FixturePage(url=f"{FIXTURE_HOST}/research/one", body=article_html("One")),
    )
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        report = await ingest_with(
            (broken, healthy),
            store,
            DocumentFetcher(client=client),
            tagger=fixture_tagger(),
        )

    by_source = {entry.source: entry for entry in report.sources}
    assert by_source["broken"].discovered == 0
    assert by_source["fixture"].stored == 1
    assert report.stored() == 1
    assert "broken" in report.summary()


async def test_a_source_whose_store_cannot_be_written_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    """An abort is a line in the report, so the loop over sources continues."""
    blocked = tmp_path / "corpus"
    blocked.write_text("not a directory", encoding="utf-8")
    declaration = fixture_declaration()
    pages = (FixturePage(url=SITEMAP, body=sitemap_xml(("/research/one",))),)
    async with client_for(pages) as client:
        report = await ingest_source(
            declaration,
            CorpusStore(root=blocked),
            DocumentFetcher(client=client),
            tagger=fixture_tagger(),
        )
    assert report.aborted
    assert "aborted" in report.summary()


# ── The walks that replaced hand-written scrapers ─────────────────────────────

PAGE_TOKEN = "2130c8d6"
LISTING = f"{FIXTURE_HOST}/research"


def listing_html(slugs: tuple[str, ...], *, paginated: bool) -> bytes:
    """A server-rendered listing page, optionally offering a next page."""
    items = "".join(f'<a href="/research-paper/{slug}">{slug}</a>' for slug in slugs)
    nextpage = f'<a href="/research?{PAGE_TOKEN}_page=2">Next</a>' if paginated else ""
    return (
        f"<html><body><nav><a href='/about'>About</a></nav>"
        f"<div class='list'>{items}</div>{nextpage}</body></html>"
    ).encode()


async def test_a_listing_avenue_paginates_until_a_page_adds_nothing() -> None:
    """The pagination token is read off the page, as the ported scraper did."""
    avenue = ListingAvenue(
        category="research", listing=LISTING, item_prefix="/research-paper/"
    )
    pages = (
        FixturePage(url=LISTING, body=listing_html(("one", "two"), paginated=True)),
        FixturePage(
            url=f"{LISTING}?{PAGE_TOKEN}_page=2",
            body=listing_html(("three",), paginated=True),
        ),
        FixturePage(
            url=f"{LISTING}?{PAGE_TOKEN}_page=3",
            body=listing_html(("three",), paginated=True),
        ),
    )
    reader = FixtureReader(pages)
    found = await avenue.discover(PageCache(reader=reader))

    assert [item.slug for item in found] == ["one", "three", "two"]
    assert found[0].url == f"{FIXTURE_HOST}/research-paper/one"
    assert f"{LISTING}?{PAGE_TOKEN}_page=3" in reader.reads
    assert f"{LISTING}?{PAGE_TOKEN}_page=4" not in reader.reads


async def test_an_unpaginated_listing_reads_one_page() -> None:
    avenue = ListingAvenue(
        category="research", listing=LISTING, item_prefix="/research-paper/"
    )
    reader = FixtureReader(
        (FixturePage(url=LISTING, body=listing_html(("only",), paginated=False)),)
    )
    found = await avenue.discover(PageCache(reader=reader))
    assert [item.slug for item in found] == ["only"]
    assert reader.reads == [LISTING]


CARDS = f"{FIXTURE_HOST}/system-cards"


async def test_a_table_avenue_takes_the_title_from_the_row() -> None:
    """A hash-named PDF has no readable slug, so the listing's label is the title."""
    avenue = TableAvenue(
        category="system-cards",
        listing=CARDS,
        link_hosts=("fixture.test",),
        link_suffixes=(".pdf",),
        link_contains=("-system-card",),
    )
    html = (
        "<html><body><table>"
        "<tr><th>Model</th><th>Card</th></tr>"
        "<tr><td>Claude Opus 4.5</td>"
        '<td><a href="https://cdn.fixture.test/9f2c1b.pdf">PDF</a></td></tr>'
        "<tr><td>Claude Sonnet 5</td>"
        '<td><a href="/claude-sonnet-5-system-card">Read</a></td></tr>'
        '<tr><td>Careers</td><td><a href="/careers">Apply</a></td></tr>'
        "</table></body></html>"
    ).encode()
    found = await avenue.discover(
        PageCache(reader=FixtureReader((FixturePage(url=CARDS, body=html),)))
    )

    by_slug = {item.slug: item for item in found}
    assert by_slug.keys() == {"9f2c1b", "claude-sonnet-5-system-card"}
    assert by_slug["9f2c1b"].title == "Claude Opus 4.5"
    assert by_slug["9f2c1b"].url == "https://cdn.fixture.test/9f2c1b.pdf"
    assert by_slug["claude-sonnet-5-system-card"].title == "Claude Sonnet 5"


async def test_a_sweep_keeps_documents_and_pdfs_but_drops_furniture() -> None:
    """A whole-site sweep wants everything except assets and taxonomy pages."""
    avenue = SweepAvenue(
        domains=(FIXTURE_HOST,),
        seeds=("https://cdn.fixture.test/policy.pdf",),
        apex="fixture.test",
    )
    body = sitemap_xml(
        (
            "/index/a-post",
            "/papers/a-paper.pdf",
            "/tag/safety",
            "/assets/app.js",
            "/",
        )
    )
    found = await avenue.discover(
        PageCache(reader=FixtureReader((FixturePage(url=SITEMAP, body=body),)))
    )

    by_slug = {item.slug: item for item in found}
    assert "index-a-post" in by_slug
    assert "papers-a-paper.pdf" in by_slug
    assert "cdn__policy.pdf" in by_slug
    assert not any("tag" in slug for slug in by_slug)
    assert not any("app.js" in slug for slug in by_slug)
    assert by_slug["index-a-post"].category == "index"


# ── Tags: a declared vocabulary, derived where the registry knows ─────────────


def test_the_vocabulary_is_declared_data_a_browse_can_read() -> None:
    """What a browse may filter on is a declaration, not what a run invented."""
    vocabulary = corpus_vocabulary()
    assert EVALUATIONS.tag == "subject:evaluations"
    assert vocabulary.holds(EVALUATIONS.tag)
    assert not vocabulary.holds("subject:whatever-a-run-felt-like")
    assert all(term.description for term in vocabulary.terms())
    assert {term.facet for term in vocabulary.terms()} == {
        "subject",
        "method",
        "organization",
    }


def test_every_declared_source_contributes_its_organization_tag() -> None:
    """The organization facet is derived from the registry, not restated."""
    vocabulary = corpus_vocabulary()
    assert {term.name for term in vocabulary.organizations} == {
        entry.key for entry in DECLARED_SOURCES
    }
    assert all(term.facet == "organization" for term in vocabulary.organizations)
    assert "organization:metr" in vocabulary.tags()


def test_a_judgement_is_only_offered_what_it_alone_can_settle() -> None:
    """Organizations are declared, so no judgement is spent re-deriving one."""
    assert {term.facet for term in corpus_vocabulary().judged_terms()} == {
        "subject",
        "method",
    }


def test_adding_a_tag_is_one_line_of_declaration() -> None:
    added = TagTerm(
        facet="subject", name="scaling-policy", description="A lab's own scaling policy"
    )
    wider = TagVocabulary(subjects=(*DEFAULT_SUBJECTS, added))
    assert wider.holds(added.tag)
    assert not DEFAULT_VOCABULARY.holds(added.tag)
    assert wider.signature() != DEFAULT_VOCABULARY.signature()


def test_rewording_a_term_changes_the_vocabulary_signature() -> None:
    """A description is what a judgement reads, so editing one is a real change."""
    reworded = DEFAULT_VOCABULARY.model_copy(
        update={
            "subjects": (
                *DEFAULT_SUBJECTS[:-1],
                DEFAULT_SUBJECTS[-1].model_copy(
                    update={"description": "Something else entirely"}
                ),
            )
        }
    )
    assert reworded.tags() == DEFAULT_VOCABULARY.tags()
    assert reworded.signature() != DEFAULT_VOCABULARY.signature()


def test_a_declared_tag_spelled_loosely_is_still_the_declared_tag() -> None:
    """Folding both sides keeps a free tag a real gap, not a capitalisation."""
    vocabulary = corpus_vocabulary()
    assert vocabulary.resolve("Subject: Evaluations") == EVALUATIONS
    assert vocabulary.resolve("chain of thought faithfulness") is None
    assert folded("Chain of Thought Faithfulness") == "chain-of-thought-faithfulness"


def test_a_per_source_fact_reaches_every_document_from_the_declaration() -> None:
    """Declared once on the source, exactly as venue and authority are."""
    metr = declaration_for("metr")
    govai = declaration_for("govai")
    assert metr is not None and govai is not None
    assert all(
        EVALUATIONS.tag in metr.tags_for(category)
        for category in ("blog", "notes", "evaluations")
    )
    assert GOVERNANCE.tag in govai.tags_for("research")
    assert "organization:govai" in govai.tags_for("analysis")


def test_an_avenue_declares_what_everything_it_publishes_is() -> None:
    """A per-category fact belongs to the avenue that publishes the category."""
    anthropic = declaration_for("anthropic")
    assert anthropic is not None
    assert SYSTEM_CARD.tag in anthropic.tags_for("system-cards")
    assert SYSTEM_CARD.tag not in anthropic.tags_for("research")
    assert anthropic.tags_for("research") == ("organization:anthropic",)


def test_every_tag_a_source_declares_is_one_the_vocabulary_holds() -> None:
    """Declarations name terms rather than strings, so this cannot drift."""
    vocabulary = corpus_vocabulary()
    declared = {
        term.tag
        for entry in DECLARED_SOURCES
        for term in (*entry.tags, *(one for a in entry.avenues for one in a.tags))
    }
    assert declared
    assert all(vocabulary.holds(tag) for tag in declared)


# ── Tags are assigned as a document enters, on every entry ────────────────────


class IngestedCorpus(BaseModel):
    """A fixture corpus that has been through one ingestion."""

    declaration: SourceDeclaration
    store: CorpusStore

    def shard(self) -> SourceShard:
        return self.store.load(self.declaration)

    def document(self, slug: str) -> StoredDocument:
        found = self.shard().stored(slug)
        assert found is not None
        return found


async def ingested_with(
    tmp_path: Path, judge: TagJudge, *, slugs: tuple[str, ...] = ("one", "two")
) -> IngestedCorpus:
    """Ingest a fixture source with ``judge`` doing the judging."""
    declaration = fixture_declaration()
    pages = (
        FixturePage(
            url=SITEMAP, body=sitemap_xml(tuple(f"/research/{one}" for one in slugs))
        ),
        *(
            FixturePage(url=f"{FIXTURE_HOST}/research/{one}", body=article_html(one))
            for one in slugs
        ),
    )
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        await ingest_source(
            declaration,
            store,
            DocumentFetcher(client=client),
            tagger=fixture_tagger(judge),
        )
    return IngestedCorpus(declaration=declaration, store=store)


async def test_every_stored_document_carries_tags(tmp_path: Path) -> None:
    """Tags are what a browse navigates by, so no document may lack them."""
    judge = FixtureJudge({"one": (EVALUATIONS.tag,)})
    ingested = await ingested_with(tmp_path, judge)

    documents = ingested.shard().documents
    assert len(documents) == 2
    assert all(document.tags.judged for document in documents)
    assert all(
        document.tags.derived == ("organization:fixture",) for document in documents
    )
    assert {document.slug for document in documents} == {
        request.slug for request in judge.requests
    }


async def test_a_document_nothing_applies_to_is_explicitly_untagged(
    tmp_path: Path,
) -> None:
    """The gap is recorded rather than absent, so a browse can see it."""
    ingested = await ingested_with(tmp_path, FixtureJudge(), slugs=("one",))

    document = ingested.document("one")
    assert document.tags.untagged()
    assert document.tags.applied() == ("organization:fixture",)

    written = json.loads(
        (tmp_path / ingested.declaration.key / "index.json").read_text(encoding="utf-8")
    )
    recorded = written["documents"][0]["tags"]
    assert recorded["judged"] is True
    assert recorded["core"] == []
    assert recorded["derived"] == ["organization:fixture"]


def test_a_never_judged_document_is_not_mistaken_for_an_untagged_one() -> None:
    """ "Nobody looked" and "looked and found nothing" are different gaps."""
    fresh = StoredDocument(slug="one", url=f"{FIXTURE_HOST}/research/one")
    assert not fresh.tags.judged
    assert not fresh.tags.untagged()
    assert DocumentTags(judged=True).untagged()


async def test_a_proposed_tag_is_kept_apart_from_the_declared_ones(
    tmp_path: Path,
) -> None:
    """A free tag is a review queue, not a filter the browse offers."""
    judge = FixtureJudge(
        {"one": ("Subject: Evaluations", "Chain of Thought Faithfulness")}
    )
    ingested = await ingested_with(tmp_path, judge, slugs=("one",))

    document = ingested.document("one")
    assert document.tags.core == (EVALUATIONS.tag,)
    assert document.tags.free == ("chain-of-thought-faithfulness",)
    assert not document.tags.untagged()
    assert not corpus_vocabulary().holds("chain-of-thought-faithfulness")


async def test_a_judgement_is_never_asked_what_the_registry_declares(
    tmp_path: Path,
) -> None:
    """Deriving-versus-judging is settled in the code, not by the caller."""
    judge = FixtureJudge()
    ingested = await ingested_with(tmp_path, judge, slugs=("one",))

    offered = {term.facet for request in judge.requests for term in request.choices}
    assert offered == {"subject", "method"}
    assert ingested.document("one").tags.derived == ("organization:fixture",)


async def test_a_pdf_gets_a_judged_summary_where_it_can_have_no_abstract(
    tmp_path: Path,
) -> None:
    """One pass tags the document and says what it is, without extracting it."""
    declaration = fixture_declaration()
    pages = (
        FixturePage(url=SITEMAP, body=sitemap_xml(("/research/paper",))),
        FixturePage(
            url=f"{FIXTURE_HOST}/research/paper",
            body=one_page_pdf(),
            content_type="application/pdf",
        ),
    )
    judge = FixtureJudge(summary="A one-page paper about evaluation harnesses.")
    store = CorpusStore(root=tmp_path)
    async with client_for(pages) as client:
        await ingest_source(
            declaration,
            store,
            DocumentFetcher(client=client),
            tagger=fixture_tagger(judge),
        )

    document = store.load(declaration).stored("paper")
    assert document is not None
    assert document.abstract == ""
    assert document.summary == "A one-page paper about evaluation harnesses."
    assert judge.requests[0].kind == "pdf"
    assert "pages='1-1'" in judge.requests[0].reading_hint()


# ── A vocabulary edit re-tags what is stored, and fetches nothing ─────────────


HARNESS_DESIGN = TagTerm(
    facet="subject",
    name="harness-design",
    description="How an evaluation harness is built, and what that changes",
)


def wider_vocabulary(added: TagTerm) -> TagVocabulary:
    """The corpus vocabulary with one more subject declared."""
    return corpus_vocabulary().model_copy(
        update={"subjects": (*DEFAULT_SUBJECTS, added)}
    )


async def test_a_vocabulary_change_retags_the_corpus_without_refetching(
    tmp_path: Path,
) -> None:
    """The whole point of the split: re-tagging reads disk, never the network.

    ``retag_source`` takes no fetcher at all, so a re-tag cannot fetch even by
    accident — and the fixture client is closed by the time this one runs.
    """
    ingested = await ingested_with(tmp_path, FixtureJudge(default=(EVALUATIONS.tag,)))

    judge = FixtureJudge(default=(HARNESS_DESIGN.tag,))
    report = await retag_source(
        ingested.declaration,
        ingested.store,
        DocumentTagger(vocabulary=wider_vocabulary(HARNESS_DESIGN), judge=judge),
    )

    assert report.examined == 2
    assert report.retagged == 2
    assert report.changed == 2
    assert report.untagged == 0
    assert report.failed == 0
    assert all(Path(request.path).is_file() for request in judge.requests)
    assert all(
        HARNESS_DESIGN.tag in document.tags.core
        for document in ingested.shard().documents
    )


async def test_a_retag_reports_how_many_documents_became_untagged(
    tmp_path: Path,
) -> None:
    """A vocabulary edit is observable: the gap it opens is counted."""
    ingested = await ingested_with(tmp_path, FixtureJudge(default=(EVALUATIONS.tag,)))

    narrowed = corpus_vocabulary().model_copy(update={"subjects": (), "methods": ()})
    report = await retag_source(
        ingested.declaration,
        ingested.store,
        DocumentTagger(vocabulary=narrowed, judge=FixtureJudge()),
    )

    assert report.changed == 2
    assert report.untagged == 2
    assert len(ingested.shard().untagged()) == 2
    assert "untagged 2" in report.summary()


async def test_documents_already_judged_against_this_vocabulary_are_left_alone(
    tmp_path: Path,
) -> None:
    """Re-running after no edit costs nothing, so re-tagging stays cheap."""
    ingested = await ingested_with(tmp_path, FixtureJudge(default=(EVALUATIONS.tag,)))

    judge = FixtureJudge(default=(EVALUATIONS.tag,))
    report = await retag_source(
        ingested.declaration, ingested.store, fixture_tagger(judge)
    )

    assert report.examined == 2
    assert report.retagged == 0
    assert report.current == 2
    assert judge.requests == []

    forced = await retag_source(
        ingested.declaration, ingested.store, fixture_tagger(judge), force=True
    )
    assert forced.retagged == 2
    assert forced.changed == 0


async def test_a_judgement_that_does_not_come_back_leaves_earlier_tags_standing(
    tmp_path: Path,
) -> None:
    """A failure today never un-tags a document that was tagged yesterday."""
    ingested = await ingested_with(
        tmp_path, FixtureJudge(default=(EVALUATIONS.tag,)), slugs=("one",)
    )

    report = await retag_source(
        ingested.declaration,
        ingested.store,
        DocumentTagger(
            vocabulary=wider_vocabulary(HARNESS_DESIGN),
            judge=FixtureJudge(answers=False),
        ),
    )

    assert report.failed == 1
    assert report.changed == 0
    document = ingested.document("one")
    assert document.tags.core == (EVALUATIONS.tag,)
    assert document.summary == "What one says."


async def test_a_document_whose_body_is_gone_is_reported_not_judged(
    tmp_path: Path,
) -> None:
    """Assignment reads bodies, so a missing one is a failure worth counting."""
    ingested = await ingested_with(
        tmp_path, FixtureJudge(default=(EVALUATIONS.tag,)), slugs=("one",)
    )
    ingested.store.document_path(
        ingested.declaration.key, ingested.document("one")
    ).unlink()

    judge = FixtureJudge(default=(HARNESS_DESIGN.tag,))
    report = await retag_source(
        ingested.declaration,
        ingested.store,
        DocumentTagger(vocabulary=wider_vocabulary(HARNESS_DESIGN), judge=judge),
        force=True,
    )

    assert report.failed == 1
    assert judge.requests == []


# ── The browse surface narrows on a tag ───────────────────────────────────────


def tagged_document(slug: str, tags: DocumentTags) -> StoredDocument:
    return StoredDocument(
        slug=slug,
        url=f"{FIXTURE_HOST}/research/{slug}",
        category="research",
        title=slug.title(),
        filename=f"{slug}.md",
        tags=tags,
    )


def test_a_browse_narrows_on_a_tag_and_still_sees_the_gap() -> None:
    """Titles alone do not narrow; a tag is what turns a scan into a filter."""
    from inkwell.corpus.retrieval import CorpusFilter, shard_entries

    shard = SourceShard(
        source="fixture",
        documents=[
            tagged_document(
                "one",
                DocumentTags(
                    derived=("organization:fixture",),
                    core=(EVALUATIONS.tag,),
                    judged=True,
                ),
            ),
            tagged_document(
                "two", DocumentTags(derived=("organization:fixture",), judged=True)
            ),
        ],
    )

    everything = shard_entries(shard, Path("/corpus/fixture"))
    narrowed = CorpusFilter(tags=(EVALUATIONS.tag,)).apply(everything)
    assert [entry.document.slug for entry in narrowed] == ["one"]
    assert narrowed[0].document.tags.applied() == (
        "organization:fixture",
        EVALUATIONS.tag,
    )

    blind_spot = CorpusFilter(untagged_only=True).apply(everything)
    assert [entry.document.slug for entry in blind_spot] == ["two"]
    assert shard.tags() == ("organization:fixture", EVALUATIONS.tag)


RSS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>CyberScoop</title>
  <item>
    <title>Irregular says human oversight responsible for AI sandbox escape</title>
    <link>https://cyberscoop.com/irregular-ai-sandbox-escape/</link>
  </item>
  <item>
    <title>Details emerge on BlackFile attacks on financial companies</title>
    <link>https://cyberscoop.com/blackfile-financial-attacks/</link>
  </item>
</channel></rss>"""

ATOM_FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>The Desk</title>
  <entry>
    <title>OpenAI agent breached Hugging Face</title>
    <link href="https://desk.example/openai-hugging-face/"/>
  </entry>
  <entry>
    <title>A story with no link</title>
  </entry>
</feed>"""


class TestFeedEntries:
    """Both syndication dialects, read without sniffing which one it is."""

    def test_rss_puts_the_url_in_link_text(self) -> None:
        entries = feed_entries(RSS_FEED)

        assert len(entries) == 2
        assert entries[0].url == "https://cyberscoop.com/irregular-ai-sandbox-escape/"
        assert entries[0].title.startswith("Irregular says")

    def test_atom_puts_the_url_in_a_link_href(self) -> None:
        entries = feed_entries(ATOM_FEED)

        assert entries[0].url == "https://desk.example/openai-hugging-face/"
        assert entries[0].title == "OpenAI agent breached Hugging Face"

    def test_an_entry_naming_no_document_is_dropped(self) -> None:
        """It points at nothing to fetch, so it is not a discovered item."""
        assert len(feed_entries(ATOM_FEED)) == 1


class TestFeedAvenue:
    """A news desk's own section, walked as the corpus's topic filter."""

    async def read(self, avenue: FeedAvenue, body: bytes) -> list[DiscoveredItem]:
        reader = FixtureReader((FixturePage(url=avenue.feed, body=body),))
        return await avenue.discover(PageCache(reader=reader))

    async def test_an_unfiltered_feed_keeps_every_entry(self) -> None:
        """Right when the feed is already the topic — an outlet's AI section."""
        avenue = FeedAvenue(category="ai", feed="https://cyberscoop.com/feed/")

        assert len(await self.read(avenue, RSS_FEED)) == 2

    async def test_required_terms_filter_before_anything_is_fetched(self) -> None:
        """The point of filtering at enumeration: the off-topic article's URL
        never reaches the fetcher at all."""
        avenue = FeedAvenue(
            category="security",
            feed="https://cyberscoop.com/feed/",
            require_terms=("ai ", "artificial intelligence"),
        )

        found = await self.read(avenue, RSS_FEED)

        assert [item.url for item in found] == [
            "https://cyberscoop.com/irregular-ai-sandbox-escape/"
        ]

    async def test_the_headline_carries_through_to_the_item(self) -> None:
        avenue = FeedAvenue(category="ai", feed="https://desk.example/feed/")

        found = await self.read(avenue, ATOM_FEED)

        assert found[0].title == "OpenAI agent breached Hugging Face"

    async def test_the_apex_is_the_articles_host_not_the_feeds(self) -> None:
        """BBC is the case: the feed is served from feeds.bbci.co.uk while the
        articles live on bbc.com, so an apex taken from the feed's own host
        would label every slug after a host no article is on."""
        avenue = FeedAvenue(
            category="technology",
            feed="https://feeds.desk.example/tech/rss.xml",
            apex="desk.example",
        )

        found = await self.read(avenue, ATOM_FEED)

        assert found[0].slug == "openai-hugging-face"
