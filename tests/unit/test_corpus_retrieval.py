"""Retrieval: navigating the index structurally, and the two layers beside it.

What is worth testing here is the promise the design makes and a later change
could quietly break — that every field the index declares is a filter and an
ordering, that the browse tier stays bodiless, that the corpus answers all of
that with no embeddings anywhere, that the phrase hunt is a separate fallback
rather than how a field question gets answered, and that a tier with nothing to
say about a document says so instead of dropping it.

The corpus is written to a real temporary directory and read back through the
shard files, so the parse, the filters, the ordering, and the file reads all
run for real. Only the embedder is a fixture — it is the seam, which is the
point of having one.
"""

import importlib
from pathlib import Path

import pytest
from pydantic import BaseModel

from inkwell.agent.stages import (
    RESEARCHER_PROMPT,
    SECTION_WRITER_PROMPT,
    SINGLE_WRITER_PROMPT,
)
from inkwell.agent.provenance import Venue
from inkwell.agent.tool_policy import research_tool_names, review_tool_names
from inkwell.agent.tools.research.corpus import CORPUS_TOOLS, corpus_search
from inkwell.corpus.quality import QualityReport
from inkwell.corpus.retrieval import (
    CorpusFilter,
    CorpusQuery,
    Ordering,
    hunt_phrase,
    page_ranges,
    read_index,
    retrieval_tier,
    search_corpus,
)
from inkwell.corpus.semantics import (
    MODEL2VEC_MODULE,
    CorpusEmbedder,
    DocumentVector,
    LocalEmbedder,
    SemanticLayer,
    VectorShard,
    cosine,
    embed_source,
    write_vectors,
)
from inkwell.corpus.storage import (
    CorpusStore,
    DiscoveredEntry,
    SourceShard,
    StoredDocument,
)
from inkwell.corpus.tags import DocumentTags

EVALUATIONS = "subject:evaluations"
GOVERNANCE = "subject:governance"

STUB_PDF = b"%PDF-1.4\ntrailer<</Root 1 0 R>>\n"
"""Enough of a PDF that a stored one is a file on disk — the page count these
tests navigate by comes from the index, which is where a reader gets it too."""


class Written(BaseModel):
    """One document and the Markdown ingestion would have stored beside it.

    Kept as a pair rather than as two lists, so a fixture cannot describe a
    document whose body it forgot to write — which is a real corpus state, and
    one the tests below reach for on purpose rather than by accident.
    """

    document: StoredDocument
    body: str = ""


def written(
    slug: str,
    *,
    title: str = "",
    category: str = "research",
    tags: tuple[str, ...] = (),
    published: str = "",
    fetched_at: str = "2026-01-01T00:00:00Z",
    quality: float = 1.0,
    kind: str = "markdown",
    abstract: str = "",
    summary: str = "",
    body: str = "",
    pages: int = 0,
) -> Written:
    """One stored document, described in the terms a retrieval test asks in."""
    return Written(
        document=StoredDocument(
            slug=slug,
            url=f"https://fixture.test/{slug}",
            category=category,
            title=title or slug,
            kind="pdf" if kind == "pdf" else "markdown",
            filename=f"{slug}.pdf" if kind == "pdf" else (f"{slug}.md" if body else ""),
            abstract=abstract,
            summary=summary,
            page_count=pages,
            published=published,
            fetched_at=fetched_at,
            quality=QualityReport(score=quality, assessed=kind != "pdf"),
            tags=DocumentTags(core=tags, judged=True),
        ),
        body=body,
    )


def write_source(
    root: Path,
    source: str,
    documents: list[Written],
    *,
    organization: str = "Fixture Institute",
    venue: str = "Fixture",
    authority: Venue = "lab_publication",
    discovered: int = 0,
) -> CorpusStore:
    """Write one source exactly as ingestion would leave it, index and bodies."""
    store = CorpusStore(root=root)
    store.save(
        SourceShard(
            source=source,
            display_name=organization,
            organization=organization,
            venue=venue,
            authority=authority,
            documents=[one.document for one in documents],
            discovered=[
                DiscoveredEntry(
                    slug=f"listed-{index}", url=f"https://fixture.test/{index}"
                )
                for index in range(discovered)
            ],
        )
    )
    for one in documents:
        if one.document.is_pdf():
            store.store_pdf(source, one.document.slug, STUB_PDF)
        elif one.body:
            store.store_markdown(source, one.document.slug, one.body)
    return store


@pytest.fixture
def corpus(tmp_path: Path) -> CorpusStore:
    """Two sources, differing on every field a query can navigate by."""
    store = write_source(
        tmp_path,
        "aisi",
        [
            written(
                "older-evaluation",
                title="Older Evaluation Report",
                tags=(EVALUATIONS,),
                published="2025-03-04",
                quality=0.9,
                abstract="An older look at how a capability was measured.",
                body="# Older\n\nThe grader disagreed with the rubric.\n",
            ),
            written(
                "newer-policy",
                title="Newer Policy Note",
                category="blog",
                tags=(GOVERNANCE,),
                published="2026-02-09",
                quality=0.5,
                abstract="A note on what a regulator asked for.",
                body="# Newer\n\nA regulator asked for an audit trail.\n",
            ),
        ],
        organization="UK AI Security Institute",
        venue="AISI Research",
        authority="official_report",
        discovered=3,
    )
    write_source(
        tmp_path,
        "metr",
        [
            written(
                "same-day-evaluation",
                title="Same Day Evaluation",
                tags=(EVALUATIONS,),
                published="2026-02-09",
                quality=0.8,
                abstract="A second measurement, published the same day.",
                body="# Same day\n\nThe grader agreed.\n",
            ),
            written(
                "a-system-card",
                title="A System Card",
                kind="pdf",
                published="2026-01-15",
                pages=47,
                summary="A judged summary standing in for an abstract a PDF cannot have.",
            ),
        ],
        organization="METR",
        venue="METR",
    )
    return store


async def answered(store: CorpusStore, query: CorpusQuery) -> list[str]:
    """The slugs one query comes back with, in the order it placed them."""
    answer = await search_corpus(query, store)
    return [hit.slug for hit in answer.documents]


# ── Field filters, one at a time and together ────────────────────────────────


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        (CorpusFilter(sources=("metr",)), ["same-day-evaluation", "a-system-card"]),
        (
            CorpusFilter(tags=(EVALUATIONS,)),
            ["same-day-evaluation", "older-evaluation"],
        ),
        (
            CorpusFilter(organizations=("METR",)),
            ["same-day-evaluation", "a-system-card"],
        ),
        (CorpusFilter(venues=("AISI Research",)), ["newer-policy", "older-evaluation"]),
        (
            CorpusFilter(authorities=("official_report",)),
            ["newer-policy", "older-evaluation"],
        ),
        (CorpusFilter(categories=("blog",)), ["newer-policy"]),
        (
            CorpusFilter(title_contains=("evaluation",)),
            ["same-day-evaluation", "older-evaluation"],
        ),
        (
            CorpusFilter(since="2026-01-01"),
            ["same-day-evaluation", "newer-policy", "a-system-card"],
        ),
        (CorpusFilter(until="2025-12-31"), ["older-evaluation"]),
        (CorpusFilter(kinds=("pdf",)), ["a-system-card"]),
        (
            CorpusFilter(min_quality=0.8),
            ["same-day-evaluation", "a-system-card", "older-evaluation"],
        ),
    ],
)
async def test_every_declared_field_is_a_filter(
    corpus: CorpusStore, where: CorpusFilter, expected: list[str]
) -> None:
    """Each field the index declares narrows on its own, through its model."""
    assert await answered(corpus, CorpusQuery(where=where)) == expected


async def test_filters_combine_into_one_question(corpus: CorpusStore) -> None:
    """Several filters at once all hold — narrowing, not a list of alternatives."""
    combined = CorpusFilter(
        tags=(EVALUATIONS,), since="2026-01-01", organizations=("METR",)
    )
    assert await answered(corpus, CorpusQuery(where=combined)) == [
        "same-day-evaluation"
    ]

    contradictory = CorpusFilter(tags=(EVALUATIONS,), categories=("blog",))
    assert await answered(corpus, CorpusQuery(where=contradictory)) == []


async def test_a_case_differs_but_the_field_still_matches(corpus: CorpusStore) -> None:
    """A recorded name is matched as a name, not as bytes the caller must spell."""
    assert await answered(
        corpus, CorpusQuery(where=CorpusFilter(organizations=("metr",)))
    ) == ["same-day-evaluation", "a-system-card"]


async def test_an_undated_document_is_placed_rather_than_dropped(
    tmp_path: Path,
) -> None:
    """No publication date is not no date: it is placed by when it was fetched."""
    store = write_source(
        tmp_path,
        "aisi",
        [
            written("undated", fetched_at="2026-05-05T00:00:00Z", body="x"),
            written("dated", published="2024-01-01", body="x"),
        ],
    )
    answer = await search_corpus(CorpusQuery(), store)
    assert [hit.slug for hit in answer.documents] == ["undated", "dated"]
    assert [hit.dated for hit in answer.documents] == [False, True]


# ── Ordering: declared, and overridable ──────────────────────────────────────


async def test_the_declared_order_is_recent_first_with_quality_breaking_ties(
    corpus: CorpusStore,
) -> None:
    """Currency is the common question; the better capture settles a tie."""
    answer = await search_corpus(CorpusQuery(), corpus)
    assert [hit.slug for hit in answer.documents] == [
        "same-day-evaluation",
        "newer-policy",
        "a-system-card",
        "older-evaluation",
    ]
    assert answer.ordering == "recency, quality (descending)"


async def test_two_documents_of_one_day_are_split_by_quality(
    corpus: CorpusStore,
) -> None:
    """The tiebreak is the recorded assessment, not the order they were read in."""
    same_day = CorpusFilter(since="2026-02-09", until="2026-02-09")
    assert await answered(corpus, CorpusQuery(where=same_day)) == [
        "same-day-evaluation",
        "newer-policy",
    ]


async def test_a_caller_overrides_the_order_and_its_direction(
    corpus: CorpusStore,
) -> None:
    """The policy is a field of the query, so disagreeing costs an argument."""
    by_title = CorpusQuery(ordering=Ordering(keys=("title",), descending=False))
    assert await answered(corpus, by_title) == [
        "a-system-card",
        "newer-policy",
        "older-evaluation",
        "same-day-evaluation",
    ]

    oldest_first = CorpusQuery(ordering=Ordering(keys=("recency",), descending=False))
    assert (await answered(corpus, oldest_first))[0] == "older-evaluation"


# ── The tiers ────────────────────────────────────────────────────────────────


async def test_a_browse_is_bodiless_but_still_carries_the_locator(
    corpus: CorpusStore,
) -> None:
    """Cheap enough to look at everything, complete enough to read from."""
    answer = await search_corpus(CorpusQuery(tier="browse"), corpus)
    assert answer.tier == "browse"
    assert all(not hit.abstract and not hit.summary for hit in answer.documents)
    assert all(not hit.reading for hit in answer.documents)
    assert all(hit.title and hit.slug and hit.source for hit in answer.documents)

    card = next(hit for hit in answer.documents if hit.slug == "a-system-card")
    assert card.path.endswith("a-system-card.pdf")
    assert card.pages == ("1-20", "21-40", "41-47")


async def test_narrowing_reads_the_abstract_or_the_summary(
    corpus: CorpusStore,
) -> None:
    """A PDF publishes no abstract, so the judged summary is what narrows it."""
    answer = await search_corpus(CorpusQuery(tier="narrow"), corpus)
    shown = {hit.slug: hit for hit in answer.documents}
    assert shown["newer-policy"].abstract.startswith("A note on what a regulator")
    assert shown["a-system-card"].abstract == ""
    assert shown["a-system-card"].summary.startswith("A judged summary")
    assert answer.gaps == 0


async def test_the_read_tier_hands_over_a_locator_not_prose(
    corpus: CorpusStore,
) -> None:
    """A full read goes to the document itself, at the pages the index knows."""
    answer = await search_corpus(
        CorpusQuery(where=CorpusFilter(kinds=("pdf",)), tier="read"), corpus
    )
    card = answer.documents[0]
    assert card.reading.startswith("Read ")
    assert "pages='1-20' then pages='21-40' then pages='41-47'" in card.reading
    assert "47-page PDF" in card.reading


def test_a_full_read_of_a_pdf_is_a_sequence_of_readable_ranges() -> None:
    """The reading tool caps a call at twenty pages, so the plan says so."""
    assert page_ranges(47) == ("1-20", "21-40", "41-47")
    assert page_ranges(20) == ("1-20",)
    assert page_ranges(0) == ()


# ── A tier that cannot answer says so ────────────────────────────────────────


async def test_a_document_with_nothing_to_narrow_on_is_reported_not_dropped(
    tmp_path: Path,
) -> None:
    """A thin corpus has to read as thin, not as a confident narrow answer."""
    store = write_source(
        tmp_path,
        "aisi",
        [
            written("has-one", abstract="Something to read.", body="x"),
            written("has-nothing", body="x"),
        ],
    )
    answer = await search_corpus(CorpusQuery(tier="narrow"), store)
    assert sorted(hit.slug for hit in answer.documents) == ["has-nothing", "has-one"]
    assert answer.gaps == 1
    bare = next(hit for hit in answer.documents if hit.slug == "has-nothing")
    assert "no abstract and no judged summary" in bare.gap


async def test_a_document_with_no_stored_body_is_reported_at_the_read_tier(
    tmp_path: Path,
) -> None:
    """The index knowing a document exists is not the same as holding it."""
    store = write_source(tmp_path, "aisi", [written("never-fetched")])
    answer = await search_corpus(CorpusQuery(tier="read"), store)
    assert [hit.slug for hit in answer.documents] == ["never-fetched"]
    assert "no body is stored" in answer.documents[0].gap
    assert answer.documents[0].reading == ""
    assert answer.gaps == 1


async def test_a_source_that_lists_more_than_it_holds_says_how_much_more(
    corpus: CorpusStore,
) -> None:
    """Two documents found means something else when three are unfetched."""
    answer = await search_corpus(
        CorpusQuery(where=CorpusFilter(sources=("aisi",))), corpus
    )
    assert answer.listed_but_absent == 3


# ── The phrase hunt, as a fallback ───────────────────────────────────────────


async def test_the_phrase_hunt_finds_a_wording_no_field_carries(
    corpus: CorpusStore,
) -> None:
    """The fallback: a turn of phrase the index declares nothing about."""
    answer = await search_corpus(CorpusQuery(phrase="audit trail"), corpus)
    assert answer.mode == "phrase"
    assert [hit.slug for hit in answer.documents] == ["newer-policy"]
    assert answer.hunt is not None
    assert answer.hunt.matches[0].line == 3
    assert "audit trail" in answer.hunt.matches[0].text


async def test_a_phrase_hunt_narrows_inside_the_structural_filter(
    corpus: CorpusStore,
) -> None:
    """The fallback composes with the fields rather than replacing them."""
    inside = CorpusQuery(where=CorpusFilter(sources=("metr",)), phrase="grader")
    assert await answered(corpus, inside) == ["same-day-evaluation"]

    everywhere = CorpusQuery(phrase="grader")
    assert sorted(await answered(corpus, everywhere)) == [
        "older-evaluation",
        "same-day-evaluation",
    ]


async def test_a_hunt_says_what_it_could_not_look_inside(corpus: CorpusStore) -> None:
    """A PDF's words are not on disk, so a miss over one is not a real miss."""
    answer = await search_corpus(CorpusQuery(phrase="capability threshold"), corpus)
    assert answer.hunt is not None
    assert answer.hunt.matches == ()
    assert answer.hunt.pdfs == 1
    assert answer.hunt.searched == 3


def test_a_hunt_reports_the_documents_it_had_no_body_for(tmp_path: Path) -> None:
    """A document listed but never fetched is named, not silently skipped."""
    store = write_source(
        tmp_path,
        "aisi",
        [written("never-fetched"), written("fetched", body="a rubric was used\n")],
    )
    hunt = hunt_phrase(read_index(store).entries, "rubric")
    assert hunt.unreadable == ("aisi/never-fetched",)
    assert [match.slug for match in hunt.matches] == ["fetched"]


def test_a_hunt_stops_at_its_cap_and_says_it_did(tmp_path: Path) -> None:
    """A phrase too common to hunt is capped, and the caller is told."""
    store = write_source(tmp_path, "aisi", [written("wordy", body="the\n" * 50)])
    hunt = hunt_phrase(read_index(store).entries, "the", limit=5)
    assert len(hunt.matches) == 5
    assert hunt.truncated


# ── The semantic layer is optional ───────────────────────────────────────────


async def test_every_structural_query_answers_with_no_embeddings_at_all(
    corpus: CorpusStore,
) -> None:
    """Not one filter, ordering, or tier takes a vector as a precondition."""
    for query in (
        CorpusQuery(),
        CorpusQuery(where=CorpusFilter(tags=(EVALUATIONS,))),
        CorpusQuery(where=CorpusFilter(since="2026-01-01"), tier="narrow"),
        CorpusQuery(tier="read", ordering=Ordering(keys=("title",))),
        CorpusQuery(phrase="grader"),
    ):
        answer = await search_corpus(query, corpus)
        assert answer.documents
        assert not answer.semantic.available


async def test_an_absent_semantic_layer_is_reported_not_raised(
    corpus: CorpusStore,
) -> None:
    """A neighbour query with the layer off still answers, and says why."""
    answer = await search_corpus(CorpusQuery(like="how is scheming measured"), corpus)
    assert answer.documents
    assert not answer.semantic.available
    assert "INKWELL_CORPUS_EMBEDDINGS" in answer.semantic.reason
    assert answer.ordering == "recency, quality (descending)"


async def test_the_layer_switched_on_with_nothing_embedded_says_which(
    corpus: CorpusStore,
) -> None:
    """Off and un-embedded are different things to do next, so they read apart."""
    layer = SemanticLayer(store=corpus, enabled=True)
    answer = await search_corpus(CorpusQuery(like="anything"), corpus, semantics=layer)
    assert "corpus embed" in answer.semantic.reason
    assert answer.documents


class KnownVector(BaseModel):
    """One text the fixture embedder already has an answer for."""

    text: str
    values: tuple[float, ...]


class FixtureEmbedder(CorpusEmbedder):
    """An embedder that answers from a table, so the seam is what is tested."""

    def __init__(self, *known: KnownVector) -> None:
        self.known = known

    def identity(self) -> str:
        return "fixture"

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            next(one.values for one in self.known if one.text == text) for text in texts
        )


class RefusingEmbedder(CorpusEmbedder):
    """An embedder whose library is not installed, as a run would find it."""

    def identity(self) -> str:
        return "refusing"

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        raise ModuleNotFoundError("No module named 'model2vec'")


async def test_the_enabled_layer_orders_by_nearness(corpus: CorpusStore) -> None:
    """Switched on and embedded, the neighbour query reorders the same set."""
    write_vectors(
        corpus,
        VectorShard(
            source="aisi",
            embedder="fixture",
            dimensions=2,
            vectors=(
                DocumentVector(slug="older-evaluation", values=(1.0, 0.0)),
                DocumentVector(slug="newer-policy", values=(0.0, 1.0)),
            ),
        ),
    )
    layer = SemanticLayer(
        store=corpus,
        enabled=True,
        embedder=FixtureEmbedder(
            KnownVector(text="measuring a capability", values=(1.0, 0.05))
        ),
    )
    answer = await search_corpus(
        CorpusQuery(
            where=CorpusFilter(sources=("aisi",)), like="measuring a capability"
        ),
        corpus,
        semantics=layer,
    )
    assert answer.semantic.available
    assert answer.semantic.embedded == 2
    assert [hit.slug for hit in answer.documents] == [
        "older-evaluation",
        "newer-policy",
    ]
    assert answer.documents[0].similarity > answer.documents[1].similarity
    assert answer.ordering == "similarity to the asked-for question"


async def test_an_embedder_that_will_not_load_costs_only_the_neighbour_query(
    corpus: CorpusStore,
) -> None:
    """The layer's worst failure is a missing ordering, never a broken browse."""
    write_vectors(
        corpus,
        VectorShard(
            source="aisi",
            embedder="fixture",
            dimensions=2,
            vectors=(DocumentVector(slug="newer-policy", values=(1.0, 0.0)),),
        ),
    )
    layer = SemanticLayer(store=corpus, enabled=True, embedder=RefusingEmbedder())
    answer = await search_corpus(
        CorpusQuery(where=CorpusFilter(sources=("aisi",)), like="anything"),
        corpus,
        semantics=layer,
    )
    assert not answer.semantic.available
    assert "uv sync --extra embeddings" in answer.semantic.reason
    assert [hit.slug for hit in answer.documents] == [
        "newer-policy",
        "older-evaluation",
    ]


def saved_static_model(directory: Path, words: tuple[str, ...]) -> Path:
    """A tiny static-embedding model written to disk, in the real library's form.

    One dimension per word and a one-hot vector for each, so a text's embedding
    is a readable count of which of them it used. Saved to a directory rather
    than pulled from the hub: what wants exercising is that this project calls
    the library the way the library expects, and a download would test the
    network instead.

    The library and its tokenizer are reached by name for the reason
    `static_encoder` gives: the extra is optional, and a checkout without it
    has to read exactly as one with it does. A static import would make the
    type checker demand a library the extra never promised to install, and
    `dev check` would fail in the state a fresh clone is in — the one state
    the test below is written to be skipped in.
    """
    import numpy

    model2vec = importlib.import_module(MODEL2VEC_MODULE)
    tokenizers = importlib.import_module("tokenizers")
    word_level = importlib.import_module("tokenizers.models").WordLevel
    whitespace = importlib.import_module("tokenizers.pre_tokenizers").Whitespace

    vocabulary = {word: index for index, word in enumerate((UNKNOWN_TOKEN, *words))}
    tokenizer = tokenizers.Tokenizer(
        word_level(vocab=vocabulary, unk_token=UNKNOWN_TOKEN)
    )
    tokenizer.pre_tokenizer = whitespace()
    model2vec.StaticModel(
        vectors=numpy.eye(len(vocabulary), dtype=numpy.float32), tokenizer=tokenizer
    ).save_pretrained(directory)
    return directory


UNKNOWN_TOKEN = "[UNK]"
"""What the fixture model calls a word it has no vector for."""


async def test_the_local_embedder_drives_the_real_library(tmp_path: Path) -> None:
    """The shipped implementation, against the library rather than a stand-in.

    Skipped where the optional extra is not installed, which is the state a
    fresh clone is in and the state every structural test above runs in.
    """
    pytest.importorskip("model2vec", reason="the embeddings extra is not installed")

    model = saved_static_model(tmp_path / "model", ("grader", "regulator"))
    embedder = LocalEmbedder(str(model))
    assert embedder.identity() == f"local:{model}"

    graded, ruled = await embedder.embed(("grader grader", "regulator"))
    assert len(graded) == 3
    assert cosine(graded, ruled) == pytest.approx(0.0)
    assert cosine(graded, graded) == pytest.approx(1.0)


async def test_the_local_embedder_answers_a_neighbour_query_end_to_end(
    corpus: CorpusStore, tmp_path: Path
) -> None:
    """Embed the corpus and ask it a question, through the shipped implementation."""
    pytest.importorskip("model2vec", reason="the embeddings extra is not installed")

    embedder = LocalEmbedder(
        str(saved_static_model(tmp_path / "model", ("capability", "regulator")))
    )
    report = await embed_source("aisi", corpus, embedder)
    assert report.embedded == 2
    assert report.embedder.startswith("local:")

    layer = SemanticLayer(store=corpus, enabled=True, embedder=embedder)
    answer = await search_corpus(
        CorpusQuery(where=CorpusFilter(sources=("aisi",)), like="regulator"),
        corpus,
        semantics=layer,
    )
    assert answer.semantic.available
    assert answer.semantic.embedder == embedder.identity()
    assert answer.documents[0].slug == "newer-policy"


async def test_an_operator_embedding_without_the_extra_is_told_what_to_install(
    corpus: CorpusStore,
) -> None:
    """A bare ModuleNotFoundError does not say that an extra is what it means."""
    report = await embed_source("aisi", corpus, RefusingEmbedder())
    assert report.embedded == 0
    assert "uv sync --extra embeddings" in report.failure
    assert "uv sync --extra embeddings" in report.summary()


async def test_embedding_skips_a_document_with_nothing_to_read(
    tmp_path: Path,
) -> None:
    """A vector from six words of title sits near everything and helps nobody."""
    store = write_source(
        tmp_path,
        "aisi",
        [
            written("has-abstract", abstract="Worth embedding.", body="x"),
            written("has-nothing", body="x"),
        ],
    )
    embedder = FixtureEmbedder(
        KnownVector(text="has-abstract\nWorth embedding.", values=(1.0,))
    )
    report = await embed_source("aisi", store, embedder)
    assert report.embedded == 1
    assert report.skipped == 1


# ── No service boundary ──────────────────────────────────────────────────────


def test_the_index_is_read_from_the_files_and_nothing_else(
    corpus: CorpusStore, tmp_path: Path
) -> None:
    """Files in, models out: no port, no daemon, nothing to have running."""
    index = read_index(corpus)
    assert {entry.source for entry in index.entries} == {"aisi", "metr"}
    assert all(entry.directory.is_relative_to(tmp_path) for entry in index.entries)

    narrowed = read_index(corpus, ("metr",))
    assert {shard.source for shard in narrowed.shards} == {"metr"}


def test_each_tier_declares_how_wide_it_answers() -> None:
    """The depths are declared, so a caller cannot invent a fourth."""
    assert retrieval_tier("browse").default_limit == 200
    assert retrieval_tier("narrow").default_limit == 25
    assert retrieval_tier("read").default_limit == 10


# ── The surface the agent sees ───────────────────────────────────────────────


def test_one_tool_answers_every_kind_of_retrieval_question() -> None:
    """Modes on one tool, so an agent picks by its question, not by guessing."""
    assert [tool.name for tool in CORPUS_TOOLS] == [
        "corpus_overview",
        "corpus_search",
        "top_up_corpus",
    ]
    described = corpus_search.description
    for asked in (
        "BROWSE BY TAG",
        "FILTER BY FIELD",
        "HUNT A PHRASE",
        "FIND A SEMANTIC NEIGHBOUR",
    ):
        assert asked in described


def test_the_description_puts_the_phrase_hunt_where_it_belongs() -> None:
    """Named as the fallback, and named as the wrong tool for a field question."""
    described = corpus_search.description
    assert "full-text fallback" in described
    assert "never the way to answer a tag, date, venue, or source question" in described


def test_the_description_says_when_to_reach_here_before_a_search_box() -> None:
    """The ordering lives in the description, which no new tool makes stale."""
    assert "BEFORE the external search boxes" in corpus_search.description


def test_the_research_and_review_stages_can_reach_the_corpus() -> None:
    """A stage cannot browse a corpus its allowlist does not name."""
    assert "mcp__research__corpus_search" in research_tool_names()
    assert "mcp__research__corpus_search" in review_tool_names()


def test_no_stage_prompt_enumerates_the_research_tools() -> None:
    """A prompt that lists tools goes stale the next time one is added."""
    for prompt in (RESEARCHER_PROMPT, SECTION_WRITER_PROMPT, SINGLE_WRITER_PROMPT):
        assert "arXiv, FRED" not in prompt
