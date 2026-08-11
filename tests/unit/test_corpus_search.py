"""Retrieval over the corpus: the two halves, the filters, and the hand-off.

Every test here runs against a corpus written to disk, because the lexical half
is a pass over stored Markdown and asserting it against anything else would be
asserting a mock. The fixture holds five documents across three sources chosen so
that each filter dimension separates them differently: one first-party lab, one
institute, one government body; one PDF; one document nobody dated.

Nothing here reaches the network. The embedding tests hold a model that projects
text onto keyword axes, which is enough to check what gets embedded, what gets
skipped, and that cosine decides the order once vectors exist.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.config import active_settings, current_settings
from inkwell.agent.tools.research.corpus import (
    CorpusOverviewInput,
    SearchCorpusInput,
    corpus_overview,
    search_corpus,
    tag_vocabulary,
)
from inkwell.corpus.embeddings import (
    EmbeddingModel,
    EmbeddingShard,
    Vector,
    cosine,
    embedding_identity,
    load_embeddings,
    metadata_surface,
    refresh_embeddings,
    surface_for,
)
from inkwell.corpus.search import (
    AUTHORITY_STANDING,
    DEFAULT_RANKING,
    Candidate,
    CorpusFilters,
    CorpusSearch,
    EmbeddedSimilarity,
    LexicalMatch,
    MetadataOverlap,
    RankingPolicy,
    ScoredCandidate,
    SearchOutcome,
    SearchRequest,
    SearchResult,
    SemanticMatch,
    corpus_candidates,
    lexical_query,
    similarity_for,
)
from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument
from inkwell.corpus.tags import declared_tag_ids, normalized_tokens
from lup.mcp import ToolError

TODAY = "2026-01-15"
"""The day every search in this file is run on, so recency never drifts."""

PDF_PROSE = "SENTENCEONLYINSIDETHEPDF"
"""A word that exists only inside the stored PDF's bytes. Retrieval must never
return it: a corpus that transcribes a PDF has stopped locating and started
extracting."""

PDF_BYTES = f"%PDF-1.4\n{PDF_PROSE} in the page stream\n".encode()


# ---------------------------------------------------------------------------
# The fixture corpus
# ---------------------------------------------------------------------------

ATLAS_CARD = StoredDocument(
    slug="atlas-3-system-card",
    url="https://alpha.example/atlas-3-system-card",
    title="Atlas 3 system card",
    category="system-cards",
    abstract=(
        "Atlas 3 is released under our frontier safety framework at the third "
        "security level."
    ),
    content_sha256="sha-atlas",
    published="2025-05-22",
    words=120,
)

ATLAS_BODY = (
    "# Atlas 3 system card\n"
    "\n"
    "## Safeguards\n"
    "\n"
    "Atlas 3 ships under our frontier safety framework, which sets the third\n"
    "security level for this deployment.\n"
    "\n"
    "## Evaluations\n"
    "\n"
    "The standard suite ran before release.\n"
)

FRONTIER_PDF = StoredDocument(
    slug="frontier-risk-report",
    url="https://alpha.example/frontier-risk-report.pdf",
    title="Frontier risk evaluation report",
    category="reports",
    kind="pdf",
    abstract="Dangerous capability evaluations across cyber and biosecurity domains.",
    content_sha256="sha-frontier",
    published="2024-11-02",
    page_count=42,
)

TASK_HORIZONS = StoredDocument(
    slug="task-horizons",
    url="https://beta.example/task-horizons",
    title="Measuring the length of tasks AI agents can complete",
    category="research",
    abstract="A capability evaluation suite for long horizon agent tasks.",
    content_sha256="sha-horizons",
    published="2025-03-19",
    words=800,
)

TASK_BODY = (
    "# Measuring the length of tasks AI agents can complete\n"
    "\n"
    "## Method\n"
    "\n"
    "We ran a capability evaluation suite over long-horizon tasks.\n"
    "\n"
    "## Results\n"
    "\n"
    "Atlas 3 completes half of the two-hour tasks.\n"
)

UNDATED_NOTE = StoredDocument(
    slug="methodology-note",
    url="https://beta.example/methodology-note",
    title="Undated methodology note",
    category="research",
    abstract="How the agent task suite was assembled.",
    content_sha256="sha-note",
    words=300,
)

UNDATED_BODY = (
    "# Undated methodology note\n\nNotes on assembling the agent task suite.\n"
)

POLICY_BRIEF = StoredDocument(
    slug="export-controls",
    url="https://gamma.example/export-controls",
    title="Export controls on advanced chips",
    category="briefs",
    abstract="Licensing thresholds for semiconductor exports and compliance regimes.",
    content_sha256="sha-brief",
    published="2023-01-15",
    words=450,
)

POLICY_BODY = (
    "# Export controls on advanced chips\n"
    "\n"
    "## Thresholds\n"
    "\n"
    "Licensing thresholds now cover semiconductor exports. The evaluation of\n"
    "capability transfer remains a policy question.\n"
)


class FixtureDocument(BaseModel):
    """One document to write into the fixture corpus: its entry and its body."""

    model_config = ConfigDict(frozen=True)

    document: StoredDocument
    body: str = ""

    def written(self, store: CorpusStore, source: str) -> StoredDocument:
        """Write this document under ``source``, returning the entry to index.

        A PDF is written as bytes and a Markdown document as text, which is the
        distinction the whole corpus turns on: only one of the two has stored
        text for the lexical half to read.
        """
        if self.document.is_pdf():
            filename = store.store_pdf(source, self.document.slug, PDF_BYTES)
        else:
            filename = store.store_markdown(source, self.document.slug, self.body)
        return self.document.model_copy(update={"filename": filename})


class FixtureSource(BaseModel):
    """One source's index, and the documents to write under it."""

    model_config = ConfigDict(frozen=True)

    shard: SourceShard
    fixtures: tuple[FixtureDocument, ...] = ()

    def key(self) -> str:
        return self.shard.source


FIXTURE_SOURCES = (
    FixtureSource(
        shard=SourceShard(
            source="alpha_lab",
            display_name="Alpha Lab",
            organization="Alpha Lab",
            venue="Alpha Lab",
            authority="first-party",
            updated_at="2026-01-10T00:00:00Z",
        ),
        fixtures=(
            FixtureDocument(document=ATLAS_CARD, body=ATLAS_BODY),
            FixtureDocument(document=FRONTIER_PDF),
        ),
    ),
    FixtureSource(
        shard=SourceShard(
            source="beta_institute",
            display_name="Beta Institute",
            organization="Beta Institute",
            venue="Beta Institute",
            authority="institute",
            updated_at="2026-01-10T00:00:00Z",
        ),
        fixtures=(
            FixtureDocument(document=TASK_HORIZONS, body=TASK_BODY),
            FixtureDocument(document=UNDATED_NOTE, body=UNDATED_BODY),
        ),
    ),
    FixtureSource(
        shard=SourceShard(
            source="gamma_gov",
            display_name="Gamma Agency",
            organization="Gamma Agency",
            venue="Gamma Agency",
            authority="government",
            updated_at="2026-01-10T00:00:00Z",
        ),
        fixtures=(FixtureDocument(document=POLICY_BRIEF, body=POLICY_BODY),),
    ),
)
"""Three sources under keys the registry does not declare, so the fixture's venue
and authority are the fixture's own rather than a declaration's."""


def write_source(store: CorpusStore, fixture: FixtureSource) -> None:
    """Write one fixture source: its documents, then the index over them."""
    store.save(
        fixture.shard.model_copy(
            update={
                "documents": [
                    entry.written(store, fixture.key()) for entry in fixture.fixtures
                ]
            }
        )
    )


@pytest.fixture
def corpus(tmp_path: Path) -> CorpusStore:
    """A corpus on disk holding every fixture source."""
    store = CorpusStore(root=tmp_path / "corpus")
    for fixture in FIXTURE_SOURCES:
        write_source(store, fixture)
    return store


async def run_search(
    corpus: CorpusStore,
    *,
    lexical: str = "",
    semantic: str = "",
    filters: CorpusFilters = CorpusFilters(),
    limit: int = 0,
) -> SearchOutcome:
    """One search with the default semantic half, on a fixed day."""
    return await CorpusSearch(store=corpus, similarity=MetadataOverlap()).run(
        SearchRequest(lexical=lexical, semantic=semantic, filters=filters, limit=limit),
        today=TODAY,
    )


def slugs(outcome: SearchOutcome) -> tuple[str, ...]:
    return tuple(result.slug for result in outcome.results)


def found_slugs(outcome: SearchOutcome) -> list[str]:
    """What a search returned, in a stable order, for comparing a whole result set."""
    return sorted(slugs(outcome))


def result_for(outcome: SearchOutcome, slug: str) -> SearchResult:
    """The one result under this slug, so a test fails loudly when it is absent."""
    found = [result for result in outcome.results if result.slug == slug]
    assert found, f"{slug} not among {slugs(outcome)}"
    return found[0]


def corpus_candidate() -> Candidate:
    """One candidate, for the policy tests that need no corpus at all."""
    return Candidate(
        source="alpha_lab",
        venue="Alpha Lab",
        authority="first-party",
        document=ATLAS_CARD,
        path=Path("atlas-3-system-card.md"),
        tags=("model-release",),
    )


# ---------------------------------------------------------------------------
# The fixture corpus is what these tests think it is
# ---------------------------------------------------------------------------


def test_the_fixture_corpus_is_files_under_one_root(corpus: CorpusStore) -> None:
    assert corpus.sources() == ("alpha_lab", "beta_institute", "gamma_gov")
    assert (corpus.root / "alpha_lab" / "index.json").is_file()
    assert (corpus.root / "alpha_lab" / "atlas-3-system-card.md").is_file()
    assert (corpus.root / "alpha_lab" / "frontier-risk-report.pdf").is_file()


def test_tags_are_read_off_metadata_rather_than_the_body(corpus: CorpusStore) -> None:
    """The tag surface is title, abstract, and category — never the stored text.

    ``atlas-3-system-card`` says "Evaluations" in a heading and nowhere in its
    metadata, so the tag it must not carry is the one its own heading names.
    """
    tagged = {
        candidate.document.slug: candidate.tags
        for candidate in corpus_candidates(corpus)
    }
    assert tagged["atlas-3-system-card"] == (
        "model-release",
        "safety-frameworks",
        "security",
    )
    assert tagged["frontier-risk-report"] == ("evaluations", "security")
    assert tagged["task-horizons"] == ("agents", "evaluations")
    assert tagged["export-controls"] == ("compute", "governance")


# ---------------------------------------------------------------------------
# The lexical half, over the stored Markdown
# ---------------------------------------------------------------------------


async def test_the_lexical_half_finds_the_document_that_says_the_words(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, lexical="frontier safety framework")
    found = result_for(outcome, "atlas-3-system-card")
    assert slugs(outcome)[0] == "atlas-3-system-card"
    assert found.lexical is not None
    assert found.lexical.matched == ("frontier", "safety", "framework")
    assert found.lexical.in_text
    assert found.semantic is None
    assert outcome.scanned == 5

    partial = result_for(outcome, "frontier-risk-report")
    assert partial.lexical is not None
    assert partial.lexical.matched == ("frontier",)


async def test_the_lexical_half_reads_the_file_it_names(corpus: CorpusStore) -> None:
    """The path a result carries is the file the wording was actually found in."""
    outcome = await run_search(corpus, lexical="semiconductor exports")
    found = result_for(outcome, "export-controls")
    assert Path(found.path).is_file()
    assert "semiconductor exports" in Path(found.path).read_text(encoding="utf-8")


async def test_a_quoted_phrase_is_required_as_a_phrase(corpus: CorpusStore) -> None:
    """``"capability evaluation"`` is not the same request as two loose words.

    ``export-controls`` says "evaluation of capability"; only ``task-horizons``
    says the phrase itself.
    """
    quoted = await run_search(corpus, lexical='"capability evaluation"')
    assert slugs(quoted) == ("task-horizons",)

    loose = await run_search(corpus, lexical="capability evaluation")
    assert "task-horizons" in slugs(loose)
    assert "export-controls" in slugs(loose)


async def test_coverage_is_the_share_of_the_phrases_a_document_has(
    corpus: CorpusStore,
) -> None:
    """Coverage rather than raw count: using every term beats repeating one."""
    outcome = await run_search(corpus, lexical="licensing semiconductor thresholds")
    found = result_for(outcome, "export-controls")
    assert found.lexical is not None
    assert found.lexical.asked == 3
    assert found.lexical.coverage() == 1.0

    partial = await run_search(corpus, lexical="licensing interpretability")
    hit = result_for(partial, "export-controls")
    assert hit.lexical is not None
    assert hit.lexical.matched == ("licensing",)
    assert hit.lexical.coverage() == 0.5


async def test_the_excerpt_is_the_matching_line_and_names_its_heading(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, lexical="two-hour")
    found = result_for(outcome, "task-horizons")
    assert found.lexical is not None
    assert found.lexical.heading == "Results"
    assert "two-hour" in found.lexical.excerpt


async def test_wording_nobody_used_returns_nothing_and_says_what_it_opened(
    corpus: CorpusStore,
) -> None:
    """An empty result is only trustworthy if it reports the ground it covered."""
    outcome = await run_search(corpus, lexical="perpetual motion")
    assert outcome.results == []
    assert outcome.held == 5
    assert outcome.admitted == 5
    assert outcome.scanned == 5


async def test_the_lexical_half_matches_a_pdf_by_title_only(
    corpus: CorpusStore,
) -> None:
    """A PDF stores no text, so title-only is the most the lexical half can say."""
    outcome = await run_search(corpus, lexical="frontier")
    found = result_for(outcome, "frontier-risk-report")
    assert found.lexical is not None
    assert not found.lexical.in_text
    assert found.lexical.excerpt == "Frontier risk evaluation report"


async def test_retrieval_never_returns_prose_out_of_a_pdf(corpus: CorpusStore) -> None:
    """The whole outcome is checked, not just the excerpt: no field may carry it."""
    stored = (corpus.root / "alpha_lab" / "frontier-risk-report.pdf").read_bytes()
    assert PDF_PROSE.encode() in stored

    outcome = await run_search(
        corpus, lexical="frontier", semantic="who evaluates dangerous capability"
    )
    assert result_for(outcome, "frontier-risk-report")
    assert PDF_PROSE not in outcome.model_dump_json()


# ---------------------------------------------------------------------------
# The semantic half, over metadata
# ---------------------------------------------------------------------------


async def test_the_semantic_half_reaches_a_document_sharing_no_wording(
    corpus: CorpusStore,
) -> None:
    """The system card answers "which commitments were published" on subject alone.

    Not one word of that question appears in the document's metadata, which is
    the case the lexical half cannot answer at all.
    """
    question = "which commitments were published"
    outcome = await run_search(corpus, semantic=question)
    found = result_for(outcome, "atlas-3-system-card")

    assert found.lexical is None
    assert found.semantic is not None
    assert found.semantic.shared_tags == ("safety-frameworks",)
    assert found.semantic.shared_terms == ()

    metadata = normalized_tokens(
        f"{ATLAS_CARD.title} {ATLAS_CARD.abstract} {ATLAS_CARD.category} Alpha Lab"
    )
    assert all(word not in metadata for word in normalized_tokens(question))


async def test_the_semantic_half_states_what_it_ranked_by(corpus: CorpusStore) -> None:
    """With no model configured the basis says so rather than implying vectors."""
    outcome = await run_search(corpus, semantic="who evaluates dangerous capability")
    assert "tag and metadata overlap" in outcome.basis
    assert "no embeddings configured" in outcome.basis


async def test_a_shared_subject_and_a_shared_word_both_count(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, semantic="dangerous capability evaluation")
    found = result_for(outcome, "frontier-risk-report")
    assert found.semantic is not None
    assert "evaluations" in found.semantic.shared_tags
    assert "capability" in found.semantic.shared_terms
    assert found.semantic.score > 0


async def test_a_question_with_no_subject_and_no_terms_reaches_nothing(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, semantic="the and of")
    assert outcome.results == []


# ---------------------------------------------------------------------------
# Merged
# ---------------------------------------------------------------------------


async def test_both_halves_come_back_as_one_ranked_list(corpus: CorpusStore) -> None:
    """One call, two questions, one result set — each result saying which half."""
    outcome = await run_search(
        corpus,
        lexical="semiconductor exports",
        semantic="which commitments were published",
    )
    assert found_slugs(outcome) == ["atlas-3-system-card", "export-controls"]

    lexical_only = result_for(outcome, "export-controls")
    assert lexical_only.lexical is not None
    assert lexical_only.semantic is None

    semantic_only = result_for(outcome, "atlas-3-system-card")
    assert semantic_only.semantic is not None
    assert semantic_only.lexical is None


async def test_a_document_both_halves_found_outranks_one_only_lexical(
    corpus: CorpusStore,
) -> None:
    """The strongest signal in the corpus is agreement between the halves."""
    outcome = await run_search(
        corpus,
        lexical="capability evaluation",
        semantic="who evaluates dangerous capability",
    )
    both = result_for(outcome, "task-horizons")
    assert both.lexical is not None
    assert both.semantic is not None
    assert slugs(outcome)[0] == "task-horizons"

    lexical_only = result_for(outcome, "export-controls")
    assert lexical_only.semantic is None
    assert both.score > lexical_only.score


def test_the_merge_pays_a_declared_bonus_when_both_halves_found_it() -> None:
    """The lift for agreement is a field, so it can be read rather than inferred."""
    policy = RankingPolicy(recency_weight=0.0, quality_weight=0.0, authority_weight=0.0)
    candidate = corpus_candidate()
    matched = LexicalMatch(matched=("safety",), asked=1)
    both = ScoredCandidate(
        candidate=candidate, lexical=matched, semantic=SemanticMatch(score=0.5)
    )
    one = ScoredCandidate(candidate=candidate, lexical=matched)

    assert both.found_by_both()
    assert not one.found_by_both()
    lifted = policy.score(both, lexical=1.0, semantic=1.0, today=TODAY)
    plain = policy.score(one, lexical=1.0, semantic=0.0, today=TODAY)
    assert lifted - plain == pytest.approx(1.0 + policy.both_halves_bonus)


async def test_a_filter_alone_browses_without_either_query(
    corpus: CorpusStore,
) -> None:
    """Naming a subject and no query is a legitimate call: list what is there."""
    outcome = await run_search(corpus, filters=CorpusFilters(tags=("governance",)))
    assert slugs(outcome) == ("export-controls",)
    assert outcome.results[0].lexical is None
    assert outcome.results[0].semantic is None


def test_a_request_with_no_query_and_no_filter_asks_nothing() -> None:
    assert SearchRequest().asks_nothing()
    assert not SearchRequest(lexical="chips").asks_nothing()
    assert not SearchRequest(semantic="who disagrees").asks_nothing()
    assert not SearchRequest(
        filters=CorpusFilters(sources=("alpha_lab",))
    ).asks_nothing()


# ---------------------------------------------------------------------------
# Every filter dimension, and combinations of them
# ---------------------------------------------------------------------------


async def test_the_source_filter_restricts_before_loading(corpus: CorpusStore) -> None:
    """Restricting by source reads that source's index and no other's."""
    narrowed = await run_search(
        corpus, lexical="semiconductor", filters=CorpusFilters(sources=("gamma_gov",))
    )
    assert slugs(narrowed) == ("export-controls",)
    assert narrowed.held == 1

    wide = await run_search(corpus, lexical="semiconductor")
    assert wide.held == 5


async def test_the_venue_filter_admits_only_that_venue(corpus: CorpusStore) -> None:
    outcome = await run_search(
        corpus, filters=CorpusFilters(venues=("Beta Institute",), tags=("agents",))
    )
    assert found_slugs(outcome) == ["methodology-note", "task-horizons"]
    assert outcome.held == 5
    assert outcome.admitted == 2


async def test_the_authority_filter_separates_a_lab_from_a_government(
    corpus: CorpusStore,
) -> None:
    first_party = await run_search(
        corpus, filters=CorpusFilters(authorities=("first-party",))
    )
    assert found_slugs(first_party) == ["atlas-3-system-card", "frontier-risk-report"]

    government = await run_search(
        corpus, filters=CorpusFilters(authorities=("government",))
    )
    assert slugs(government) == ("export-controls",)


async def test_the_category_filter_admits_one_section_of_a_source(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(
        corpus, filters=CorpusFilters(categories=("system-cards",))
    )
    assert slugs(outcome) == ("atlas-3-system-card",)


async def test_the_tag_filter_requires_any_of_the_subjects(
    corpus: CorpusStore,
) -> None:
    one = await run_search(corpus, filters=CorpusFilters(tags=("compute",)))
    assert slugs(one) == ("export-controls",)

    either = await run_search(
        corpus, filters=CorpusFilters(tags=("compute", "model-release"))
    )
    assert found_slugs(either) == ["atlas-3-system-card", "export-controls"]


async def test_the_since_bound_admits_only_what_is_newer(corpus: CorpusStore) -> None:
    outcome = await run_search(corpus, filters=CorpusFilters(since="2025-01-01"))
    assert found_slugs(outcome) == ["atlas-3-system-card", "task-horizons"]


async def test_the_until_bound_admits_only_what_is_older(corpus: CorpusStore) -> None:
    outcome = await run_search(corpus, filters=CorpusFilters(until="2024-12-31"))
    assert found_slugs(outcome) == ["export-controls", "frontier-risk-report"]


async def test_the_two_date_bounds_together_are_a_window(corpus: CorpusStore) -> None:
    outcome = await run_search(
        corpus, filters=CorpusFilters(since="2024-01-01", until="2024-12-31")
    )
    assert slugs(outcome) == ("frontier-risk-report",)


async def test_a_document_nobody_dated_is_dropped_and_counted(
    corpus: CorpusStore,
) -> None:
    """Under a date bound an undated document is not an answer, and not silent."""
    bounded = await run_search(corpus, filters=CorpusFilters(since="2020-01-01"))
    assert "methodology-note" not in slugs(bounded)
    assert bounded.undated == 1

    unbounded = await run_search(corpus, filters=CorpusFilters(tags=("agents",)))
    assert "methodology-note" in slugs(unbounded)
    assert unbounded.undated == 0


async def test_filters_combine_across_every_dimension(corpus: CorpusStore) -> None:
    """Every dimension in force at once, and only the document meeting all of them."""
    outcome = await run_search(
        corpus,
        filters=CorpusFilters(
            sources=("alpha_lab", "beta_institute"),
            venues=("Beta Institute",),
            authorities=("institute",),
            categories=("research",),
            tags=("evaluations",),
            since="2025-01-01",
            until="2025-12-31",
        ),
    )
    assert slugs(outcome) == ("task-horizons",)
    assert outcome.held == 4


async def test_a_filter_that_admits_nothing_still_reports_the_corpus(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, filters=CorpusFilters(venues=("Nowhere",)))
    assert outcome.results == []
    assert outcome.held == 5
    assert outcome.admitted == 0


async def test_a_filter_narrows_a_query_rather_than_replacing_it(
    corpus: CorpusStore,
) -> None:
    wide = await run_search(corpus, lexical="capability evaluation")
    assert "task-horizons" in slugs(wide)

    narrowed = await run_search(
        corpus,
        lexical="capability evaluation",
        filters=CorpusFilters(authorities=("government",)),
    )
    assert slugs(narrowed) == ("export-controls",)


async def test_the_outcome_names_the_filters_in_force(corpus: CorpusStore) -> None:
    """A result set has to explain its own size to be trusted."""
    outcome = await run_search(
        corpus, filters=CorpusFilters(tags=("compute",), since="2020-01-01")
    )
    assert "tags=compute" in outcome.filters
    assert "since=2020-01-01" in outcome.filters


def test_a_date_bound_is_a_string_comparison_over_iso_order() -> None:
    filters = CorpusFilters(since="2025-01-01", until="2025-12-31")
    assert filters.in_range("2025-06-01")
    assert not filters.in_range("2024-12-31")
    assert not filters.in_range("2026-01-01")
    assert not filters.in_range("")
    assert CorpusFilters().in_range("")


# ---------------------------------------------------------------------------
# The hand-off: a locator, never a passage
# ---------------------------------------------------------------------------


async def test_a_pdf_result_carries_a_page_range_and_its_page_count(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, lexical="frontier")
    found = result_for(outcome, "frontier-risk-report")
    assert found.kind == "pdf"
    assert found.locator.kind == "pages"
    assert found.page_count == 42
    assert "pages='1-20'" in found.read_next
    assert found.path.endswith("frontier-risk-report.pdf")


async def test_a_short_pdf_asks_only_for_the_pages_it_has(tmp_path: Path) -> None:
    """The window is a ceiling, not a demand for pages that do not exist."""
    store = CorpusStore(root=tmp_path / "corpus")
    write_source(
        store,
        FixtureSource(
            shard=SourceShard(
                source="alpha_lab", venue="Alpha Lab", authority="first-party"
            ),
            fixtures=(
                FixtureDocument(
                    document=FRONTIER_PDF.model_copy(update={"page_count": 5})
                ),
            ),
        ),
    )
    outcome = await run_search(store, lexical="frontier")
    assert "pages='1-5'" in result_for(outcome, "frontier-risk-report").read_next


async def test_a_markdown_result_carries_the_line_and_the_heading(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, lexical="frontier safety framework")
    found = result_for(outcome, "atlas-3-system-card")
    assert found.locator.kind == "section"
    assert "Safeguards" in found.read_next
    assert found.path.endswith("atlas-3-system-card.md")

    assert found.lexical is not None
    lines = Path(found.path).read_text(encoding="utf-8").splitlines()
    assert "frontier safety framework" in lines[found.lexical.line - 1]


async def test_a_semantic_only_markdown_result_says_to_read_from_the_top(
    corpus: CorpusStore,
) -> None:
    """No wording was matched, so there is no line to claim — and it says so."""
    outcome = await run_search(corpus, semantic="which commitments were published")
    found = result_for(outcome, "atlas-3-system-card")
    assert found.locator.kind == "section"
    assert "from the top" in found.read_next


async def test_a_result_carries_what_a_citation_needs_without_the_document(
    corpus: CorpusStore,
) -> None:
    outcome = await run_search(corpus, lexical="frontier safety framework")
    found = result_for(outcome, "atlas-3-system-card")
    assert found.venue == "Alpha Lab"
    assert found.authority == "first-party"
    assert found.date == "2025-05-22"
    assert found.published == "2025-05-22"
    assert found.url == ATLAS_CARD.url
    assert found.abstract == ATLAS_CARD.abstract


# ---------------------------------------------------------------------------
# Ranking is a declared policy, not a constant in the merge
# ---------------------------------------------------------------------------


def test_the_ranking_policy_is_data_a_caller_can_replace() -> None:
    """Every number deciding an order is a field, so a caller states its own."""
    assert DEFAULT_RANKING.lexical_weight == 1.0
    assert DEFAULT_RANKING.semantic_weight == 1.0

    mine = RankingPolicy(semantic_weight=4.0, limit=3)
    assert mine.semantic_weight == 4.0
    assert mine.limit == 3
    assert DEFAULT_RANKING.semantic_weight == 1.0


def test_recency_falls_to_zero_at_the_declared_window() -> None:
    policy = RankingPolicy(recency_window_days=100)
    assert policy.recency("2026-01-15", today=TODAY) == 1.0
    assert policy.recency("2025-12-31", today=TODAY) == pytest.approx(0.85)
    assert policy.recency("2020-01-01", today=TODAY) == 0.0
    assert policy.recency("", today=TODAY) == 0.0


def test_standing_is_a_declared_table_with_a_floor_for_the_unlisted() -> None:
    standing = RankingPolicy().standing_of("first-party")
    assert standing == AUTHORITY_STANDING["first-party"]
    assert RankingPolicy(standing={}).standing_of("institute") == 0.5


def test_the_policy_scales_each_half_against_its_own_best() -> None:
    """A coverage fraction and a cosine are not the same kind of number, so
    neither half's raw scale is allowed to decide the order."""
    policy = RankingPolicy(
        recency_weight=0.0,
        quality_weight=0.0,
        authority_weight=0.0,
        both_halves_bonus=0.0,
    )
    ranked = policy.merged(
        [
            ScoredCandidate(
                candidate=corpus_candidate(), semantic=SemanticMatch(score=0.04)
            )
        ],
        today=TODAY,
    )
    assert ranked[0].score == pytest.approx(1.0)


async def test_a_declared_policy_changes_the_order(corpus: CorpusStore) -> None:
    """The same two halves, ranked twice, ordered differently on purpose."""
    request = SearchRequest(
        lexical="capability evaluation", semantic="which commitments were published"
    )
    lexical_led = await CorpusSearch(
        store=corpus,
        similarity=MetadataOverlap(),
        ranking=RankingPolicy(semantic_weight=0.0, both_halves_bonus=0.0),
    ).run(request, today=TODAY)
    semantic_led = await CorpusSearch(
        store=corpus,
        similarity=MetadataOverlap(),
        ranking=RankingPolicy(lexical_weight=0.0, both_halves_bonus=0.0),
    ).run(request, today=TODAY)

    assert slugs(semantic_led)[0] == "atlas-3-system-card"
    assert slugs(lexical_led)[0] != "atlas-3-system-card"


async def test_the_outcome_states_how_it_was_ranked(corpus: CorpusStore) -> None:
    outcome = await run_search(corpus, lexical="frontier")
    assert "lexical×1.0" in outcome.ranked_by
    assert "semantic×1.0" in outcome.ranked_by


async def test_the_limit_is_the_policy_default_until_a_call_overrides_it(
    corpus: CorpusStore,
) -> None:
    everything = await run_search(corpus, filters=CorpusFilters(sources=("alpha_lab",)))
    assert len(everything.results) == 2

    one = await run_search(
        corpus, filters=CorpusFilters(sources=("alpha_lab",)), limit=1
    )
    assert len(one.results) == 1


# ---------------------------------------------------------------------------
# Embeddings: optional, over metadata, recomputed only on a changed identity
# ---------------------------------------------------------------------------

VECTOR_AXES = (
    "safety",
    "framework",
    "evaluation",
    "export",
    "chip",
    "agent",
    "commitment",
)
"""The keyword axes the fixture model projects onto — enough for cosine to
separate the fixture documents without a network."""


def fixture_vector(text: str) -> Vector:
    """A deterministic stand-in for an embedding: one axis per keyword."""
    tokens = normalized_tokens(text)
    return tuple(1.0 if axis in tokens else 0.0 for axis in VECTOR_AXES)


class FixtureEmbeddings(EmbeddingModel):
    """An embedding model that records what it was asked to embed.

    Recording is what makes the skip checkable: a run that re-embeds nothing has
    to have *asked* for nothing, not merely reported zero.
    """

    identifier: str = "fixture-embedder"
    asked: list[tuple[str, ...]] = Field(default_factory=list)

    async def embed(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        self.asked.append(texts)
        return tuple(fixture_vector(text) for text in texts)

    def texts(self) -> tuple[str, ...]:
        return tuple(text for batch in self.asked for text in batch)


def shard_of(corpus: CorpusStore, source: str) -> SourceShard:
    shard = corpus.load_by_name(source)
    assert shard is not None
    return shard


async def embed_everything(corpus: CorpusStore, model: EmbeddingModel) -> None:
    """Bring every fixture source's vectors up to date."""
    for fixture in FIXTURE_SOURCES:
        await refresh_embeddings(corpus, shard_of(corpus, fixture.key()), model)


async def test_what_gets_embedded_is_metadata_and_the_abstract(
    corpus: CorpusStore,
) -> None:
    """Never the body: a PDF has no stored text, so full text would cover half
    the corpus and silently not the other half."""
    model = FixtureEmbeddings()
    await refresh_embeddings(corpus, shard_of(corpus, "alpha_lab"), model)

    embedded = model.texts()
    assert len(embedded) == 2
    card = next(text for text in embedded if ATLAS_CARD.title in text)
    assert "venue: Alpha Lab" in card
    assert "section: system-cards" in card
    assert "tags: model-release, safety-frameworks, security" in card
    assert ATLAS_CARD.abstract in card
    assert "Safeguards" not in card
    assert PDF_PROSE not in "".join(embedded)


async def test_an_unchanged_corpus_embeds_nothing(corpus: CorpusStore) -> None:
    """The point of keying on identity: keeping a still corpus searchable is free."""
    shard = shard_of(corpus, "alpha_lab")
    first = await refresh_embeddings(corpus, shard, FixtureEmbeddings())
    assert first.embedded == 2
    assert first.unchanged == 0

    again = FixtureEmbeddings()
    second = await refresh_embeddings(corpus, shard, again)
    assert second.embedded == 0
    assert second.unchanged == 2
    assert again.texts() == ()


async def test_only_the_document_whose_content_changed_is_re_embedded(
    corpus: CorpusStore,
) -> None:
    shard = shard_of(corpus, "alpha_lab")
    await refresh_embeddings(corpus, shard, FixtureEmbeddings())

    rewritten = shard.model_copy(
        update={
            "documents": [
                document.model_copy(update={"content_sha256": "sha-atlas-rewritten"})
                if document.slug == "atlas-3-system-card"
                else document
                for document in shard.documents
            ]
        }
    )
    model = FixtureEmbeddings()
    refresh = await refresh_embeddings(corpus, rewritten, model)
    assert refresh.embedded == 1
    assert refresh.unchanged == 1
    assert len(model.texts()) == 1
    assert ATLAS_CARD.title in model.texts()[0]


def test_the_identity_covers_the_embedded_text_and_not_only_the_content(
    corpus: CorpusStore,
) -> None:
    """Changing what a document is *about* invalidates its vector too, which is
    what makes the tag vocabulary safe to edit."""
    shard = shard_of(corpus, "alpha_lab")
    document = shard.documents_by_slug()["atlas-3-system-card"]
    surface = surface_for(document, venue=shard.venue)
    assert embedding_identity(document.content_sha256, surface) != embedding_identity(
        document.content_sha256, f"{surface}\ntags: something-else"
    )
    assert embedding_identity("other-sha", surface) != embedding_identity(
        document.content_sha256, surface
    )


async def test_changing_the_model_invalidates_every_vector(
    corpus: CorpusStore,
) -> None:
    shard = shard_of(corpus, "alpha_lab")
    await refresh_embeddings(corpus, shard, FixtureEmbeddings())

    refresh = await refresh_embeddings(
        corpus, shard, FixtureEmbeddings(identifier="other-embedder")
    )
    assert refresh.embedded == 2
    assert refresh.unchanged == 0
    assert load_embeddings(corpus, "alpha_lab").model == "other-embedder"


async def test_vectors_are_stored_beside_the_index_they_derive_from(
    corpus: CorpusStore,
) -> None:
    await refresh_embeddings(corpus, shard_of(corpus, "alpha_lab"), FixtureEmbeddings())
    stored = corpus.embeddings_path("alpha_lab")
    assert stored == corpus.root / "alpha_lab" / "embeddings.json"
    assert stored.is_file()

    shard = load_embeddings(corpus, "alpha_lab")
    assert sorted(entry.slug for entry in shard.documents) == [
        "atlas-3-system-card",
        "frontier-risk-report",
    ]
    assert all(entry.identity for entry in shard.documents)


async def test_deleting_the_vectors_leaves_the_corpus_searchable(
    corpus: CorpusStore,
) -> None:
    """Vectors are derived data: losing them costs sharpness, never capability."""
    await refresh_embeddings(corpus, shard_of(corpus, "alpha_lab"), FixtureEmbeddings())
    corpus.embeddings_path("alpha_lab").unlink()

    outcome = await run_search(corpus, semantic="which commitments were published")
    assert "atlas-3-system-card" in slugs(outcome)
    assert load_embeddings(corpus, "alpha_lab").documents == []


async def test_cosine_over_stored_vectors_ranks_the_semantic_half(
    corpus: CorpusStore,
) -> None:
    """Where vectors exist the basis says cosine, and the order follows it."""
    model = FixtureEmbeddings()
    await embed_everything(corpus, model)

    outcome = await CorpusSearch(
        store=corpus,
        similarity=EmbeddedSimilarity(
            model=model,
            shards={
                fixture.key(): load_embeddings(corpus, fixture.key())
                for fixture in FIXTURE_SOURCES
            },
        ),
    ).run(SearchRequest(semantic="export chip"), today=TODAY)

    assert "cosine over metadata embeddings" in outcome.basis
    assert slugs(outcome)[0] == "export-controls"
    assert outcome.unplaced == 0


async def test_a_document_with_no_vector_is_unplaced_rather_than_unrelated(
    corpus: CorpusStore,
) -> None:
    """ "No vector" and "not similar" are different facts, and are reported so."""
    outcome = await CorpusSearch(
        store=corpus,
        similarity=EmbeddedSimilarity(
            model=FixtureEmbeddings(),
            shards={"alpha_lab": EmbeddingShard(source="alpha_lab")},
        ),
    ).run(
        SearchRequest(
            semantic="safety framework", filters=CorpusFilters(sources=("alpha_lab",))
        ),
        today=TODAY,
    )
    assert outcome.results == []
    assert outcome.unplaced == 2


async def test_a_configured_model_with_no_vectors_still_searches(
    corpus: CorpusStore,
) -> None:
    """Turning embeddings on is an improvement, never a prerequisite."""
    outcome = await CorpusSearch(
        store=corpus, similarity=similarity_for(corpus, (), model=FixtureEmbeddings())
    ).run(SearchRequest(semantic="which commitments were published"), today=TODAY)

    assert "tag and metadata overlap" in outcome.basis
    assert "atlas-3-system-card" in slugs(outcome)


async def test_retrieval_uses_the_vectors_once_they_exist(corpus: CorpusStore) -> None:
    await embed_everything(corpus, FixtureEmbeddings())
    outcome = await CorpusSearch(
        store=corpus, similarity=similarity_for(corpus, (), model=FixtureEmbeddings())
    ).run(SearchRequest(semantic="export chip"), today=TODAY)

    assert "cosine over metadata embeddings" in outcome.basis
    assert slugs(outcome)[0] == "export-controls"


def test_cosine_refuses_to_compare_what_it_cannot() -> None:
    assert cosine((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine((1.0, 0.0), (0.0, 1.0)) == 0.0
    assert cosine((1.0, 0.0), (1.0, 0.0, 0.0)) == 0.0
    assert cosine((), ()) == 0.0
    assert cosine((0.0, 0.0), (1.0, 1.0)) == 0.0


def test_the_embedded_surface_is_labelled_rather_than_concatenated() -> None:
    """A label is context a model reads: ``venue: Alpha Lab`` places the document."""
    surface = metadata_surface(
        title="Atlas 3 system card", venue="Alpha Lab", tags=("model-release",)
    )
    assert surface.splitlines() == [
        "title: Atlas 3 system card",
        "venue: Alpha Lab",
        "tags: model-release",
    ]


# ---------------------------------------------------------------------------
# The tool surface
# ---------------------------------------------------------------------------


def test_the_tool_takes_both_queries_and_every_filter_in_one_call() -> None:
    """One surface: the two halves and all the filters are one input model."""
    request = SearchCorpusInput(
        lexical='"safety case"',
        semantic="who argues the other side",
        sources=("metr",),
        venues=("METR",),
        authorities=("institute",),
        categories=("research",),
        tags=("evaluations",),
        since="2025-01-01",
        until="2025-12-31",
        limit=5,
    ).request()

    assert lexical_query(request.lexical).phrases == (("safety", "case"),)
    assert request.semantic == "who argues the other side"
    assert request.filters.sources == ("metr",)
    assert request.filters.venues == ("METR",)
    assert request.filters.authorities == ("institute",)
    assert request.filters.categories == ("research",)
    assert request.filters.tags == ("evaluations",)
    assert request.filters.since == "2025-01-01"
    assert request.filters.until == "2025-12-31"
    assert request.limit == 5


@pytest.fixture
def rooted_at(corpus: CorpusStore) -> Iterator[CorpusStore]:
    """Point the process's corpus root at the fixture, as a session would."""
    token = active_settings.set(
        current_settings().model_copy(update={"corpus_path": str(corpus.root)})
    )
    try:
        yield corpus
    finally:
        active_settings.reset(token)


async def test_the_tool_answers_both_halves_in_one_call(
    rooted_at: CorpusStore,
) -> None:
    """End to end through the tool the agent actually calls."""
    outcome = await search_corpus(
        SearchCorpusInput(
            lexical="semiconductor exports",
            semantic="which commitments were published",
            since="2020-01-01",
        )
    )
    assert found_slugs(outcome) == ["atlas-3-system-card", "export-controls"]
    assert result_for(outcome, "export-controls").lexical is not None
    assert result_for(outcome, "atlas-3-system-card").semantic is not None
    assert outcome.ranked_by
    assert "since=2020-01-01" in outcome.filters


async def test_the_tool_hands_a_pdf_over_with_its_page_range(
    rooted_at: CorpusStore,
) -> None:
    outcome = await search_corpus(SearchCorpusInput(lexical="frontier"))
    found = result_for(outcome, "frontier-risk-report")
    assert found.locator.kind == "pages"
    assert "pages='1-20'" in found.read_next
    assert PDF_PROSE not in outcome.model_dump_json()


async def test_the_tool_refuses_a_call_that_asks_nothing(
    rooted_at: CorpusStore,
) -> None:
    """No query and no filter is not an empty corpus, it is an empty question."""
    with pytest.raises(ToolError, match="Ask for something"):
        await search_corpus(SearchCorpusInput())


async def test_the_overview_publishes_the_vocabulary_retrieval_filters_by(
    rooted_at: CorpusStore,
) -> None:
    """The tags filter is only usable if something enumerates the subjects."""
    overview = await corpus_overview(CorpusOverviewInput())
    assert [entry.tag_id for entry in overview.tags] == list(declared_tag_ids())


def test_the_tool_refuses_a_source_the_registry_does_not_declare() -> None:
    """Silently returning nothing would look exactly like an empty corpus."""
    with pytest.raises(ToolError, match="No such corpus source"):
        SearchCorpusInput(sources=("nowhere",)).request()


def test_the_tool_refuses_a_tag_outside_the_vocabulary() -> None:
    with pytest.raises(ToolError, match="No such corpus tag"):
        SearchCorpusInput(tags=("not-a-subject",)).request()


def test_the_tool_refuses_a_date_bound_that_is_not_a_date() -> None:
    with pytest.raises(ToolError, match="is not a date"):
        SearchCorpusInput(since="last tuesday").request()
    with pytest.raises(ToolError, match="is not a date"):
        SearchCorpusInput(until="2025-13-40").request()


def test_the_vocabulary_the_overview_publishes_names_each_subject_once() -> None:
    """``model-release`` is reached by a category rule and a wording rule both."""
    vocabulary = tag_vocabulary()
    ids = [entry.tag_id for entry in vocabulary]
    assert len(ids) == len(dict.fromkeys(ids))
    assert "model-release" in ids
    assert all(entry.description for entry in vocabulary)
