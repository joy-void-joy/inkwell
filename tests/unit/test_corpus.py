"""The corpus: declarations, incremental enumeration, storage, and resilience.

What is worth testing here is the behaviour the design promises and a later
change could quietly break — that a re-run adds only what is new, that the index
round-trips, that a PDF is never turned into text, and that one dead source
costs only itself. The network is faked with a fixture reader and an httpx mock
transport, so the real extraction, hashing, and merge paths all run.
"""

from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from inkwell.corpus.discovery import (
    Avenue,
    DiscoveredItem,
    DiscoveryOutcome,
    ListingAvenue,
    PageCache,
    PageReader,
    SitemapAvenue,
    SweepAvenue,
    TableAvenue,
    discover_avenues,
    source_id_for,
)
from inkwell.corpus.fetch import DocumentFetcher
from inkwell.corpus.ingest import ingest_source, ingest_with
from inkwell.corpus.quality import DEFAULT_RULES, ThinRule, assess
from inkwell.corpus.registry import (
    DECLARED_SOURCES,
    DROPPED_SOURCES,
    SourceDeclaration,
    active_declarations,
    declaration_for,
)
from inkwell.corpus.storage import (
    CorpusStore,
    SourceShard,
    StoredDocument,
    merged_discovery,
    pending_entries,
)

FIXTURE_HOST = "https://fixture.test"
SITEMAP = f"{FIXTURE_HOST}/sitemap.xml"

PDF_BYTES_SAMPLE = b"%PDF-1.4\ntrailer<</Root 1 0 R>>\n"
"""Enough of a PDF to be recognised as one, for the refusal check."""


def one_page_pdf() -> bytes:
    """A real one-page PDF, so reading its page count exercises the real path."""
    import pymupdf

    with pymupdf.open() as document:
        document.new_page()
        return bytes(document.tobytes())


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
        authority="institute",
        hosts=("fixture.test",),
        avenues=avenues or (SitemapAvenue(category="research", sitemap=SITEMAP),),
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


def test_the_seven_proven_sources_are_active() -> None:
    """The sources the ported database actually enumerated are the active ones."""
    assert {entry.key for entry in active_declarations()} == {
        "aisi",
        "anthropic",
        "apollo",
        "epoch",
        "govai",
        "iaps",
        "metr",
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
    """Authority is derived from the declaration, never asserted per citation."""
    for entry in DECLARED_SOURCES:
        assert entry.venue
        assert entry.organization
        assert entry.authority in (
            "first-party",
            "government",
            "institute",
            "independent",
        )


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
    js_hard = [entry for entry in DECLARED_SOURCES if entry.renders_with_javascript]
    assert {entry.key for entry in js_hard} == {"deepmind", "xai"}


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
    kinds = (SitemapAvenue, ListingAvenue, TableAvenue, SweepAvenue)
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
        report = await ingest_source(declaration, store, DocumentFetcher(client=client))

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
            declaration, store, DocumentFetcher(client=client), limit=1
        )
        assert first.discovered == 2
        assert first.new == 2
        assert first.stored == 1
        assert first.skipped == 1

        second = await ingest_source(declaration, store, DocumentFetcher(client=client))
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
        await ingest_source(declaration, store, DocumentFetcher(client=client))
        again = await ingest_source(
            declaration, store, DocumentFetcher(client=client), refresh=True
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
        report = await ingest_source(declaration, store, DocumentFetcher(client=client))

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
            (broken, healthy), store, DocumentFetcher(client=client)
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
            declaration, CorpusStore(root=blocked), DocumentFetcher(client=client)
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
