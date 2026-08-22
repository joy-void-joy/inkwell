"""What the citation checks say about a citation set, and what they never do.

Three properties carry most of the weight. The distribution is read off the fields
research recorded, so a section whose sources carry none reports as unknown rather
than as a handful of distinct values that look like spread — the failure a check
counting URLs would make. Both checks are advisory: a leaning citation set, a
one-sided claim, and a judge that dies mid-verdict all leave the run going, with
the rows reaching the stage that rewrites the prose and the author's document.
And the scope a lean is measured over reaches past the run: quoting one forum
over and over is a claim about the whole book, which every chapter of it can sit
under its own ceilings while making.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from lup.actors.cohort import ActorCohort
from lup.actors.refs import ActorRef
from lup.runtime.models import SessionId, TurnId, TurnIdentifiers, TurnResult
from lup.types import Usage
from pydantic import BaseModel, ValidationError

from inkwell.agent import config as config_mod
from inkwell.agent.cohort import run_cohort
from inkwell.agent.book import (
    BookCitations,
    BookStore,
    ChapterCitations,
    ChapterPlacement,
)
from inkwell.agent.diversity import (
    DEFAULT_DIVERSITY_RULES,
    WHOLE_BOOK,
    WHOLE_WORK,
    AuthorAxis,
    AxisTally,
    ContestedClaim,
    ContestedSignal,
    DiversityReport,
    DiversityRules,
    DomainAxis,
    JudgedClaim,
    OneSidedVerdict,
    OrganizationAxis,
    book_scope,
    contested_claims,
    distribution_report,
    judged_report,
    merge_reports,
)
from inkwell.agent.models import (
    ArticlePlan,
    ResearchCompilation,
    ResearchFinding,
    ResearchQuestion,
    ResearchSource,
    SectionDraft,
    SectionPlan,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    CITATION_DIVERSITY_ARTIFACT,
    check_citation_diversity,
    record_citations,
    rewrite_final,
)
from inkwell.agent.provenance import SourceProvenance, Venue


def recorded(
    url: str,
    *,
    domain: str = "",
    organization: str = "",
    authors: list[str] | None = None,
    venue: Venue = "preprint",
    published: str = "2024",
    title: str = "A document",
) -> ResearchSource:
    """One cited source carrying the provenance research recorded for it."""
    return ResearchSource(
        title=title,
        url=url,
        relevance="It bears on the claim",
        key_excerpt="A quoted line",
        provenance=SourceProvenance(
            venue=venue,
            role="primary",
            published=published,
            acquired_via="url_fetch",
            domain=domain,
            organization=organization,
            authors=authors if authors is not None else [],
        ),
    )


def unrecorded(url: str, *, title: str = "A document") -> ResearchSource:
    """One cited source from an artifact written before provenance was recorded."""
    return ResearchSource(
        title=title,
        url=url,
        relevance="It bears on the claim",
        key_excerpt="A quoted line",
    )


def finding(
    *sources: ResearchSource,
    question: str = "What does the evidence say?",
    confidence: float = 0.9,
) -> ResearchFinding:
    """One research finding over the given citations."""
    return ResearchFinding(
        question=question,
        answer="What research concluded",
        sources=list(sources),
        confidence=confidence,
    )


def compiled(*findings: ResearchFinding) -> ResearchCompilation:
    """The research artifact holding these findings."""
    return ResearchCompilation(findings=list(findings))


def planned(*questions: ResearchQuestion, sections: list[str]) -> ArticlePlan:
    """A plan whose sections own the given research questions."""
    return ArticlePlan(
        title="A piece",
        thesis="A thesis",
        target_format="blog",
        sections=[
            SectionPlan(title=title, summary="What it covers", key_points=["A point"])
            for title in sections
        ],
        research_questions=list(questions),
        source_quotes=[],
        author_direction="Write it well",
        voice_notes="Plain",
    )


def finished_turn[T: BaseModel | None](output: T = None) -> TurnResult[T]:
    """A stage agent that ran, submitting whatever it was asked for."""
    return TurnResult(
        output=output,
        messages=[],
        blocks=[],
        usage=Usage(),
        duration=timedelta(),
        identifiers=TurnIdentifiers(
            session=SessionId(value="session"), turn=TurnId(value="turn")
        ),
    )


@pytest.fixture
def cohort(tmp_path: Path) -> ActorCohort:
    """The population the judges are members of, rooted in this test's tree."""
    return run_cohort(tmp_path / "artifacts")


def judging(
    cohort: ActorCohort,
    monkeypatch: pytest.MonkeyPatch,
    verdict: OneSidedVerdict | None = None,
    error: Exception | None = None,
) -> None:
    """Stand in for every judge's turn, with one verdict or one failure.

    Replaces the ask rather than the session beneath it, which is the seam a
    caller of the cohort actually has: what these tests are about is what the
    check does with a verdict, a missing verdict, and a judge that died.
    """

    async def asked(
        actor: ActorRef, *_args: object, **_kwargs: object
    ) -> TurnResult[OneSidedVerdict | None]:
        cohort.spawn(actor, "judge")
        if error is not None:
            raise error
        return finished_turn(verdict)

    monkeypatch.setattr(cohort, "ask", asked)


BOOK = "textbook"
"""The book every chapter in these tests belongs to."""

CHAPTER_3 = ChapterPlacement(book=BOOK, chapter=3)
"""The chapter the run under test is writing, where it is writing one."""


def cited_by(ordinal: int, *sources: ResearchSource) -> ChapterCitations:
    """One chapter's citation record, as that chapter's own run left it."""
    return ChapterCitations(
        placement=ChapterPlacement(book=BOOK, chapter=ordinal),
        citations=[source.provenance for source in sources],
    )


def book_of(*chapters: ChapterCitations) -> BookCitations:
    """What the book holds — only the chapters that have run."""
    return BookCitations(book=BOOK, chapters=list(chapters))


def chapter_sources(ordinal: int) -> list[ResearchSource]:
    """One chapter's citations: half one forum, half hosts of its own.

    Exactly half, which is what the chapter's domain ceiling lets pass, so a
    chapter shaped like this reports nothing about its own domains.
    """
    return [
        *(
            recorded(f"https://lesswrong.com/{ordinal}/{n}", domain="lesswrong.com")
            for n in range(2)
        ),
        *(
            recorded(
                f"https://host{ordinal}{n}.example/a",
                domain=f"host{ordinal}{n}.example",
            )
            for n in range(2)
        ),
    ]


def domains(report: DiversityReport, scope: str) -> AxisTally:
    """How one scope of a report distributes over domains."""
    return next(
        tally
        for tally in report.tallies
        if tally.scope == scope and tally.axis == "domain"
    )


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BookStore:
    """The book store every part of the run resolves, pointed at a temp root."""
    root = tmp_path / "books"
    monkeypatch.setattr(config_mod.settings, "books_path", str(root))
    return BookStore(root=root)


# ---------------------------------------------------------------------------
# The distribution, computed from what was recorded
# ---------------------------------------------------------------------------


def test_distribution_counts_the_recorded_domains() -> None:
    report = distribution_report(
        compiled(
            finding(
                recorded("https://arxiv.org/abs/1", domain="arxiv.org"),
                recorded("https://arxiv.org/abs/2", domain="arxiv.org"),
                recorded("https://arxiv.org/abs/3", domain="arxiv.org"),
                recorded("https://nature.com/a", domain="nature.com"),
            )
        )
    )
    tally = next(t for t in report.tallies if t.axis == "domain")

    assert tally.scope == WHOLE_WORK
    assert tally.recorded == 4
    assert tally.unrecorded == 0
    assert [(share.value, share.count) for share in tally.shares] == [
        ("arxiv.org", 3),
        ("nature.com", 1),
    ]
    assert tally.share() == pytest.approx(0.75)


def test_distribution_reads_the_recorded_field_rather_than_the_url() -> None:
    """The recorded domain decides, so nothing re-parses a URL to disagree."""
    report = distribution_report(
        compiled(finding(recorded("https://arxiv.org/abs/1", domain="nature.com")))
    )
    tally = next(t for t in report.tallies if t.axis == "domain")

    assert [share.value for share in tally.shares] == ["nature.com"]


def test_a_leaning_domain_reports_a_named_row() -> None:
    report = distribution_report(
        compiled(
            finding(
                *(
                    recorded(f"https://epoch.ai/{n}", domain="epoch.ai")
                    for n in range(3)
                ),
                recorded("https://nature.com/a", domain="nature.com"),
            )
        )
    )
    row = next(row for row in report.rows if row.check == "domain concentration")

    assert row.scope == WHOLE_WORK
    assert "epoch.ai" in row.detail
    assert "3 of 4" in row.detail
    assert not row.missing_side


def test_organization_and_author_leans_each_report() -> None:
    report = distribution_report(
        compiled(
            finding(
                *(
                    recorded(
                        f"https://openai.com/{n}",
                        domain=f"host{n}.example",
                        organization="OpenAI",
                        authors=["Ada L."],
                    )
                    for n in range(3)
                ),
                recorded(
                    "https://nature.com/a",
                    domain="nature.com",
                    organization="Nature",
                    authors=["Grace H."],
                ),
            )
        )
    )
    reported = {row.check for row in report.rows}

    assert "organization concentration" in reported
    assert "author concentration" in reported
    assert "domain concentration" not in reported


def test_a_spread_citation_set_reports_nothing() -> None:
    report = distribution_report(
        compiled(
            finding(
                *(
                    recorded(
                        f"https://host{n}.example/a",
                        domain=f"host{n}.example",
                        organization=f"Org {n}",
                        authors=[f"Author {n}"],
                        venue="peer_reviewed" if n % 2 else "preprint",
                        published=f"202{n}",
                    )
                    for n in range(4)
                )
            )
        )
    )

    assert report.rows == []
    assert report.tallies


def test_two_citations_are_too_few_to_call_a_lean() -> None:
    """Any two-source set looks like half, so the floor keeps that quiet."""
    report = distribution_report(
        compiled(
            finding(
                recorded("https://arxiv.org/abs/1", domain="arxiv.org"),
                recorded("https://arxiv.org/abs/2", domain="arxiv.org"),
            )
        )
    )

    assert [row.check for row in report.rows if "concentration" in row.check] == []


# ---------------------------------------------------------------------------
# Unrecorded provenance is unknown, not diversity
# ---------------------------------------------------------------------------


def test_unrecorded_provenance_reports_unknown_rather_than_diverse() -> None:
    """Four sources nobody characterized are one unknown, not four values."""
    report = distribution_report(
        compiled(finding(*(unrecorded(f"https://host{n}.example/a") for n in range(4))))
    )
    tally = next(t for t in report.tallies if t.axis == "domain")
    row = next(row for row in report.rows if row.check == "domain unrecorded")

    assert tally.recorded == 0
    assert tally.unrecorded == 4
    assert tally.shares == []
    assert tally.silent()
    assert not tally.leans(DEFAULT_DIVERSITY_RULES.min_citations)
    assert "unknown, not spread" in row.detail
    assert "domain concentration" not in {reported.check for reported in report.rows}


def test_an_unrecorded_organization_is_unknown_even_with_domains_recorded() -> None:
    report = distribution_report(
        compiled(
            finding(
                *(
                    recorded(f"https://host{n}.example/a", domain=f"host{n}.example")
                    for n in range(3)
                )
            )
        )
    )
    reported = {row.check for row in report.rows}

    assert next(t for t in report.tallies if t.axis == "organization").silent()
    assert "organization unrecorded" in reported
    assert "domain unrecorded" not in reported


def test_an_unknown_venue_and_an_undated_source_count_as_unrecorded() -> None:
    report = distribution_report(
        compiled(
            finding(
                *(
                    recorded(
                        f"https://host{n}.example/a",
                        domain=f"host{n}.example",
                        venue="unknown",
                        published="undated",
                    )
                    for n in range(3)
                )
            )
        )
    )

    assert next(t for t in report.tallies if t.axis == "venue").silent()
    assert next(t for t in report.tallies if t.axis == "date").silent()


# ---------------------------------------------------------------------------
# Scope: per section, and the whole work
# ---------------------------------------------------------------------------


def test_each_section_is_tallied_from_the_questions_asked_for_it() -> None:
    research = compiled(
        finding(
            recorded("https://arxiv.org/abs/1", domain="arxiv.org"),
            question="How fast did capability grow?",
        ),
        finding(
            recorded("https://nature.com/a", domain="nature.com"),
            question="What did the review conclude?",
        ),
    )
    plan = planned(
        ResearchQuestion(question="How fast did capability grow?", section="Growth"),
        ResearchQuestion(question="What did the review conclude?", section="Reception"),
        sections=["Growth", "Reception"],
    )
    report = distribution_report(research, plan=plan)
    growth = next(
        t for t in report.tallies if t.scope == "Growth" and t.axis == "domain"
    )

    assert {tally.scope for tally in report.tallies} == {
        WHOLE_WORK,
        "Growth",
        "Reception",
    }
    assert [share.value for share in growth.shares] == ["arxiv.org"]


def test_a_draft_citing_another_sections_source_counts_it_for_both() -> None:
    borrowed = recorded("https://nature.com/a", domain="nature.com")
    research = compiled(
        finding(
            recorded("https://arxiv.org/abs/1", domain="arxiv.org"),
            question="How fast did capability grow?",
        ),
        finding(borrowed, question="What did the review conclude?"),
    )
    plan = planned(
        ResearchQuestion(question="How fast did capability grow?", section="Growth"),
        ResearchQuestion(question="What did the review conclude?", section="Reception"),
        sections=["Growth", "Reception"],
    )
    drafts = {
        "Growth": SectionDraft(
            title="Growth", content="A section", sources_used=[borrowed.url]
        )
    }
    report = distribution_report(research, plan=plan, drafts=drafts)
    growth = next(
        t for t in report.tallies if t.scope == "Growth" and t.axis == "domain"
    )

    assert {share.value for share in growth.shares} == {"arxiv.org", "nature.com"}


# ---------------------------------------------------------------------------
# Scope: the whole book, which no chapter of it can see
# ---------------------------------------------------------------------------


def test_the_book_leans_on_a_domain_no_chapter_of_it_leans_on() -> None:
    """The reason the scope exists. Every chapter puts half its citations on one
    forum, which every chapter's own ceiling lets pass; the book is then half
    one forum, and only the wider scope has anything to say about it."""
    book = book_of(*(cited_by(n, *chapter_sources(n)) for n in (1, 2, 3)))
    report = distribution_report(compiled(finding(*chapter_sources(3))), book=book)
    leaning = [row for row in report.rows if row.check == "domain concentration"]

    assert domains(report, WHOLE_WORK).share() == pytest.approx(0.5)
    assert not domains(report, WHOLE_WORK).leans(DEFAULT_DIVERSITY_RULES.min_citations)
    assert domains(report, book_scope(BOOK)).recorded == 12
    assert [row.scope for row in leaning] == [book_scope(BOOK)]
    assert "lesswrong.com" in leaning[0].detail
    assert "6 of 12" in leaning[0].detail


def test_a_book_row_names_a_scope_no_chapter_could_be_mistaken_for() -> None:
    """The job the scope field does: a rewrite can act on its own chapter's
    lean and cannot act on the book's, so it has to be able to tell them apart."""
    book = book_of(cited_by(3, *chapter_sources(3)))
    report = distribution_report(compiled(finding(*chapter_sources(3))), book=book)
    rendered = report.render()

    assert book_scope(BOOK) != WHOLE_WORK
    assert {WHOLE_WORK, book_scope(BOOK)} <= {tally.scope for tally in report.tallies}
    assert f"[{WHOLE_BOOK} — {BOOK}]" in rendered
    assert f"[{WHOLE_WORK}]" in rendered


def test_a_run_placed_in_no_book_reports_exactly_its_own_scopes() -> None:
    research = compiled(
        finding(
            *chapter_sources(1),
            question="How fast did capability grow?",
        )
    )
    plan = planned(
        ResearchQuestion(question="How fast did capability grow?", section="Growth"),
        sections=["Growth"],
    )
    report = distribution_report(research, plan=plan)

    assert {tally.scope for tally in report.tallies} == {WHOLE_WORK, "Growth"}
    assert WHOLE_BOOK not in report.render()
    assert (
        report.render() == distribution_report(research, plan=plan, book=None).render()
    )


def test_a_chapter_that_has_not_run_contributes_nothing() -> None:
    """Chapter two has written nothing down, so the book is what one and three
    cited — not a chapter of nothing counted against them."""
    book = book_of(cited_by(1, *chapter_sources(1)), cited_by(3, *chapter_sources(3)))
    report = distribution_report(compiled(finding(*chapter_sources(3))), book=book)
    tally = domains(report, book_scope(BOOK))

    assert (tally.recorded, tally.unrecorded) == (8, 0)
    assert tally.share() == pytest.approx(0.5)


def test_a_chapter_that_cited_nothing_adds_no_unrecorded_citations() -> None:
    book = book_of(cited_by(1, *chapter_sources(1)), cited_by(2))
    report = distribution_report(compiled(finding(*chapter_sources(1))), book=book)
    tally = domains(report, book_scope(BOOK))

    assert (tally.recorded, tally.unrecorded) == (4, 0)


def test_an_unrecorded_chapter_reports_unknown_at_book_scope() -> None:
    """The same refusal to re-derive provenance, one scope wider: four sources
    nobody characterized are one unknown, not four hosts that look like spread."""
    book = book_of(
        cited_by(1, *(unrecorded(f"https://host{n}.example/a") for n in range(4)))
    )
    report = distribution_report(
        compiled(
            finding(*(unrecorded(f"https://host{n}.example/a") for n in range(4)))
        ),
        book=book,
    )
    tally = domains(report, book_scope(BOOK))
    row = next(
        row
        for row in report.rows
        if row.scope == book_scope(BOOK) and row.check == "domain unrecorded"
    )

    assert tally.silent()
    assert tally.shares == []
    assert "unknown, not spread" in row.detail


def test_an_unrecorded_chapter_does_not_dilute_a_recorded_ones_lean() -> None:
    """What a check that re-derived provenance from URLs would get wrong: the
    chapter nobody recorded is unknown, so it neither hides nor spreads the lean."""
    book = book_of(
        cited_by(
            1,
            *(
                recorded(f"https://lesswrong.com/{n}", domain="lesswrong.com")
                for n in range(3)
            ),
        ),
        cited_by(2, *(unrecorded(f"https://host{n}.example/a") for n in range(3))),
    )
    tally = domains(
        distribution_report(compiled(finding()), book=book), book_scope(BOOK)
    )

    assert (tally.recorded, tally.unrecorded) == (3, 3)
    assert tally.share() == pytest.approx(1.0)


def test_the_books_ceilings_are_stricter_than_a_chapters_by_default() -> None:
    """Spread costs less over more citations, so the same share reports at the
    wider scope and passes at the narrower one."""
    strict = DEFAULT_DIVERSITY_RULES.book_axes
    lenient = DEFAULT_DIVERSITY_RULES.axes

    assert [axis.name for axis in strict] == [axis.name for axis in lenient]
    assert all(
        book.share_ceiling < chapter.share_ceiling
        for book, chapter in zip(strict, lenient, strict=True)
    )


def test_the_book_ceiling_is_an_overridable_default() -> None:
    book = book_of(*(cited_by(n, *chapter_sources(n)) for n in (1, 2, 3)))
    research = compiled(finding(*chapter_sources(3)))
    lenient = DiversityRules(
        axes=[DomainAxis()], book_axes=[DomainAxis(share_ceiling=0.9)]
    )

    assert "domain concentration" in {
        row.check for row in distribution_report(research, book=book).rows
    }
    assert distribution_report(research, book=book, rules=lenient).rows == []


def test_the_citation_floor_guards_small_numbers_at_book_scope() -> None:
    """Two citations across a whole book always look like a lean, so the floor
    that keeps a chapter quiet has to keep the book quiet too."""
    book = book_of(
        *(
            cited_by(n, recorded(f"https://lesswrong.com/{n}", domain="lesswrong.com"))
            for n in (1, 2)
        )
    )
    research = compiled(
        finding(recorded("https://lesswrong.com/2", domain="lesswrong.com"))
    )

    def leaning(min_citations: int) -> list[str]:
        """Which scopes report a lean when this many citations are enough."""
        rules = DiversityRules(
            axes=[DomainAxis()],
            book_axes=[DomainAxis(share_ceiling=0.35)],
            min_citations=min_citations,
        )
        return [
            row.scope
            for row in distribution_report(research, book=book, rules=rules).rows
        ]

    assert leaning(3) == []
    assert leaning(2) == [book_scope(BOOK)]


# ---------------------------------------------------------------------------
# What the book scope reads, and what it costs
# ---------------------------------------------------------------------------


def test_a_chapter_records_what_it_cited_as_its_research_completes(
    store: BookStore,
) -> None:
    record_citations(CHAPTER_3, compiled(finding(*chapter_sources(3))))
    held = store.citations(BOOK).chapters

    assert [record.placement for record in held] == [CHAPTER_3]
    assert [each.domain for each in held[0].citations if each is not None] == [
        "lesswrong.com",
        "lesswrong.com",
        "host30.example",
        "host31.example",
    ]


def test_a_standalone_run_records_no_citations(store: BookStore) -> None:
    record_citations(None, compiled(finding(*chapter_sources(1))))

    assert store.citations(BOOK).chapters == []


def test_the_book_is_measured_from_records_rather_than_from_research(
    store: BookStore,
) -> None:
    """What the whole scope rests on: re-running the check on every draft reads
    small per-chapter records, never every chapter's research compilation."""
    record_citations(
        CHAPTER_3,
        compiled(
            finding(
                recorded(
                    "https://lesswrong.com/a",
                    domain="lesswrong.com",
                    title="A very long document",
                )
            )
        ),
    )
    written = store.citations_path(CHAPTER_3).read_text(encoding="utf-8")

    assert "lesswrong.com" in written
    assert "A very long document" not in written
    assert "A quoted line" not in written


async def test_the_check_weighs_the_book_the_run_is_placed_in(
    notes: PipelineNotes, store: BookStore, cohort: ActorCohort
) -> None:
    for ordinal in (1, 2, 3):
        record_citations(
            ChapterPlacement(book=BOOK, chapter=ordinal),
            compiled(finding(*chapter_sources(ordinal))),
        )
    report = await check_citation_diversity(
        notes,
        compiled(finding(*chapter_sources(3))),
        placement=CHAPTER_3,
        judge=False,
        cohort=cohort,
    )
    artifact = notes.text_artifact_path(CITATION_DIVERSITY_ARTIFACT)

    assert [
        row.scope for row in report.rows if row.check == "domain concentration"
    ] == [book_scope(BOOK)]
    assert book_scope(BOOK) in artifact.read_text(encoding="utf-8")


async def test_a_run_placed_nowhere_is_weighed_against_no_book(
    notes: PipelineNotes, store: BookStore, cohort: ActorCohort
) -> None:
    record_citations(
        ChapterPlacement(book=BOOK, chapter=1),
        compiled(
            finding(
                *(
                    recorded(f"https://lesswrong.com/{n}", domain="lesswrong.com")
                    for n in range(4)
                )
            )
        ),
    )
    report = await check_citation_diversity(
        notes, compiled(finding(*chapter_sources(3))), judge=False, cohort=cohort
    )

    assert {tally.scope for tally in report.tallies} == {WHOLE_WORK}
    assert {row.scope for row in report.rows} == {WHOLE_WORK}


async def test_a_leaning_book_never_fails_the_check_and_stays_off_the_document(
    notes: PipelineNotes,
    store: BookStore,
    cohort: ActorCohort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A book-wide lean is a row for the rewrite, exactly as a chapter's is; the
    author is asked only about the side nobody cited."""
    judging(
        cohort,
        monkeypatch,
        verdict=OneSidedVerdict(
            one_sided=True, missing_side="the other camp", reason="All one way."
        ),
    )
    for ordinal in (1, 2, 3):
        record_citations(
            ChapterPlacement(book=BOOK, chapter=ordinal),
            compiled(finding(*chapter_sources(ordinal), confidence=0.2)),
        )
    report = await check_citation_diversity(
        notes,
        compiled(finding(*chapter_sources(3), confidence=0.2)),
        placement=CHAPTER_3,
        cohort=cohort,
    )
    reported = {row.scope for row in report.rows}

    assert book_scope(BOOK) in reported
    assert [note.note for note in report.author_notes()] == [
        note.note for note in report.author_notes() if "the other camp" in note.note
    ]
    assert report.author_notes()


# ---------------------------------------------------------------------------
# Thresholds are overridable defaults
# ---------------------------------------------------------------------------


def test_a_lower_ceiling_reports_a_lean_the_default_lets_pass() -> None:
    research = compiled(
        finding(
            recorded("https://arxiv.org/abs/1", domain="arxiv.org"),
            recorded("https://arxiv.org/abs/2", domain="arxiv.org"),
            recorded("https://nature.com/a", domain="nature.com"),
            recorded("https://science.org/a", domain="science.org"),
        )
    )
    strict = DiversityRules(axes=[DomainAxis(share_ceiling=0.4)])
    by_default = [
        row for row in distribution_report(research).rows if "domain" in row.check
    ]
    reported = distribution_report(research, rules=strict).rows

    assert by_default == []
    assert [row.check for row in reported] == ["domain concentration"]
    assert "40%" in reported[0].detail


def test_a_raised_ceiling_silences_a_lean_the_default_reports() -> None:
    research = compiled(
        finding(
            *(
                recorded(f"https://arxiv.org/abs/{n}", domain="arxiv.org")
                for n in range(3)
            ),
            recorded("https://nature.com/a", domain="nature.com"),
        )
    )
    lenient = DiversityRules(axes=[DomainAxis(share_ceiling=0.9)])

    assert "domain concentration" in {
        row.check for row in distribution_report(research).rows
    }
    assert distribution_report(research, rules=lenient).rows == []


def test_the_citation_floor_is_overridable() -> None:
    research = compiled(
        finding(
            recorded("https://arxiv.org/abs/1", domain="arxiv.org"),
            recorded("https://arxiv.org/abs/2", domain="arxiv.org"),
        )
    )
    counted = DiversityRules(axes=[DomainAxis()], min_citations=2)

    assert distribution_report(research, rules=counted).rows[0].check == (
        "domain concentration"
    )


def test_a_caller_can_measure_its_own_axes_only() -> None:
    research = compiled(
        finding(
            *(
                recorded(
                    f"https://host{n}.example/a",
                    domain=f"host{n}.example",
                    organization="RAND",
                )
                for n in range(3)
            )
        )
    )
    rules = DiversityRules(axes=[OrganizationAxis(), AuthorAxis()])
    report = distribution_report(research, rules=rules)

    assert {tally.axis for tally in report.tallies} == {"organization", "author"}


# ---------------------------------------------------------------------------
# The contested signal is declared, not re-guessed
# ---------------------------------------------------------------------------


def test_low_confidence_marks_a_claim_contested() -> None:
    research = compiled(
        finding(recorded("https://arxiv.org/abs/1"), confidence=0.4, question="Is it?"),
        finding(recorded("https://arxiv.org/abs/2"), confidence=0.95, question="When?"),
    )

    assert [claim.finding.question for claim in contested_claims(research)] == [
        "Is it?"
    ]


def test_several_citations_mark_a_confident_claim_contested() -> None:
    research = compiled(
        finding(
            *(recorded(f"https://arxiv.org/abs/{n}") for n in range(3)),
            confidence=1.0,
            question="Stacked?",
        )
    )

    assert [claim.finding.question for claim in contested_claims(research)] == [
        "Stacked?"
    ]


def test_a_finding_with_no_sources_has_no_citation_set_to_judge() -> None:
    assert contested_claims(compiled(finding(confidence=0.1))) == []


def test_the_contested_signal_is_overridable() -> None:
    research = compiled(finding(recorded("https://arxiv.org/abs/1"), confidence=0.4))
    narrow = DiversityRules(
        contested=ContestedSignal(confidence_below=0.2, several_citations=10)
    )

    assert contested_claims(research) != []
    assert contested_claims(research, rules=narrow) == []


def test_a_contested_claim_carries_its_section() -> None:
    research = compiled(
        finding(
            recorded("https://arxiv.org/abs/1"),
            confidence=0.3,
            question="How fast did capability grow?",
        )
    )
    plan = planned(
        ResearchQuestion(question="How fast did capability grow?", section="Growth"),
        sections=["Growth"],
    )

    assert contested_claims(research, plan=plan)[0].scope == "Growth"


def test_the_judge_reads_each_cited_source_with_its_provenance() -> None:
    claim = ContestedClaim(
        scope="Growth",
        finding=finding(
            recorded(
                "https://epoch.ai/a",
                domain="epoch.ai",
                organization="Epoch AI",
                published="2024-03",
            ),
            question="Is progress accelerating?",
            confidence=0.4,
        ),
    )
    brief = claim.render()

    assert "Is progress accelerating?" in brief
    assert "Epoch AI" in brief
    assert "2024" in brief
    assert "A quoted line" in brief


# ---------------------------------------------------------------------------
# The judged pass: one side, and which one is missing
# ---------------------------------------------------------------------------


def test_a_one_sided_claim_reports_a_row_naming_the_missing_side() -> None:
    judged = JudgedClaim(
        claim=ContestedClaim(
            scope="Growth",
            finding=finding(
                recorded("https://epoch.ai/a"),
                question="Is progress accelerating?",
                confidence=0.4,
            ),
        ),
        verdict=OneSidedVerdict(
            one_sided=True,
            missing_side=(
                "economists who read the same productivity data as stagnation"
            ),
            reason="Every cited source is a scaling proponent.",
        ),
    )
    report = judged_report([judged])
    row = report.rows[0]

    assert report.judged == 1
    assert row.check == "position diversity"
    assert row.scope == "Growth"
    assert "stagnation" in row.missing_side
    assert row.anchor == "Is progress accelerating?"


def test_a_two_sided_claim_reports_nothing_but_is_still_counted() -> None:
    judged = JudgedClaim(
        claim=ContestedClaim(scope="Growth", finding=finding(recorded("https://a/b"))),
        verdict=OneSidedVerdict(one_sided=False),
    )
    report = judged_report([judged])

    assert report.rows == []
    assert report.judged == 1


def test_a_one_sided_verdict_must_name_the_missing_side() -> None:
    with pytest.raises(ValidationError, match="requires missing_side"):
        OneSidedVerdict(one_sided=True)


def test_a_missing_side_becomes_a_note_on_the_authors_document() -> None:
    report = judged_report(
        [
            JudgedClaim(
                claim=ContestedClaim(
                    scope="Growth",
                    finding=finding(
                        recorded("https://a/b"), question="Is progress accelerating?"
                    ),
                ),
                verdict=OneSidedVerdict(
                    one_sided=True, missing_side="the stagnation reading"
                ),
            )
        ]
    )
    note = report.author_notes()[0]

    assert note.stage == "review:diversity"
    assert "the stagnation reading" in note.note
    assert note.anchor == "Is progress accelerating?"


def test_a_computed_lean_is_not_worth_the_authors_attention() -> None:
    """Only a missing side becomes a comment; a lean is for the rewrite to weigh."""
    report = distribution_report(
        compiled(
            finding(
                *(
                    recorded(f"https://arxiv.org/abs/{n}", domain="arxiv.org")
                    for n in range(3)
                ),
                recorded("https://nature.com/a", domain="nature.com"),
            )
        )
    )

    assert report.rows
    assert report.author_notes() == []


# ---------------------------------------------------------------------------
# Advisory, not gating
# ---------------------------------------------------------------------------


async def test_a_leaning_citation_set_never_fails_the_check(
    notes: PipelineNotes, cohort: ActorCohort, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row at once, and the check still returns a report."""
    judging(
        cohort,
        monkeypatch,
        verdict=OneSidedVerdict(
            one_sided=True, missing_side="the other camp", reason="All one way."
        ),
    )
    research = compiled(
        finding(
            *(unrecorded(f"https://arxiv.org/abs/{n}") for n in range(4)),
            confidence=0.2,
        )
    )

    report = await check_citation_diversity(notes, research, cohort=cohort)

    assert report.rows
    assert report.judged == 1
    assert report.author_notes()


async def test_a_judge_that_dies_costs_its_row_and_nothing_more(
    notes: PipelineNotes, cohort: ActorCohort, monkeypatch: pytest.MonkeyPatch
) -> None:
    judging(
        cohort,
        monkeypatch,
        error=RuntimeError("Agent error: You've hit your limit"),
    )
    research = compiled(
        finding(
            *(
                recorded(f"https://arxiv.org/abs/{n}", domain="arxiv.org")
                for n in range(3)
            ),
            recorded("https://nature.com/a", domain="nature.com"),
            confidence=0.2,
        )
    )

    report = await check_citation_diversity(notes, research, cohort=cohort)

    assert "domain concentration" in {row.check for row in report.rows}
    assert "position diversity" not in {row.check for row in report.rows}
    assert report.author_notes() == []


async def test_a_judge_reaching_no_verdict_leaves_the_computed_rows(
    notes: PipelineNotes, cohort: ActorCohort, monkeypatch: pytest.MonkeyPatch
) -> None:
    judging(cohort, monkeypatch)
    research = compiled(finding(unrecorded("https://a/b"), confidence=0.2))

    report = await check_citation_diversity(notes, research, cohort=cohort)

    assert report.rows
    assert report.judged == 0


async def test_the_judged_pass_is_what_a_cheap_run_turns_off(
    notes: PipelineNotes, cohort: ActorCohort, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distribution costs only the counting, so it runs either way."""
    judging(cohort, monkeypatch, error=AssertionError("no judge should be spent"))
    research = compiled(finding(unrecorded("https://a/b"), confidence=0.1))

    report = await check_citation_diversity(notes, research, judge=False, cohort=cohort)

    assert report.judged == 0
    assert report.rows


# ---------------------------------------------------------------------------
# The rows reach the stage that rewrites the prose
# ---------------------------------------------------------------------------


async def test_the_rewrite_stage_reads_the_rows(
    notes: PipelineNotes, cohort: ActorCohort, monkeypatch: pytest.MonkeyPatch
) -> None:
    await check_citation_diversity(
        notes,
        compiled(
            finding(
                *(
                    recorded(f"https://arxiv.org/abs/{n}", domain="arxiv.org")
                    for n in range(3)
                ),
                recorded("https://nature.com/a", domain="nature.com"),
            )
        ),
        judge=False,
        cohort=cohort,
    )

    draft_path = notes.draft_path("merged")
    draft_path.write_text("# A draft\n\nProse.", encoding="utf-8")
    output_path = notes.draft_path("final")
    tasks: list[str] = []

    async def capture(task: str, **_kwargs: object) -> TurnResult[None]:
        tasks.append(task)
        output_path.write_text("# A draft\n\nFinal prose.", encoding="utf-8")
        return finished_turn()

    monkeypatch.setattr("inkwell.agent.pipeline.query", capture)
    await rewrite_final(notes, draft_path, [], output_path=output_path)

    artifact = notes.text_artifact_path(CITATION_DIVERSITY_ARTIFACT)

    assert str(artifact) in tasks[0]
    assert "Citation diversity" in tasks[0]
    assert "arxiv.org" in artifact.read_text(encoding="utf-8")


async def test_the_rewrite_stage_says_nothing_about_rows_never_written(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_path = notes.draft_path("merged")
    draft_path.write_text("# A draft\n\nProse.", encoding="utf-8")
    output_path = notes.draft_path("final")
    tasks: list[str] = []

    async def capture(task: str, **_kwargs: object) -> TurnResult[None]:
        tasks.append(task)
        output_path.write_text("# A draft\n\nFinal prose.", encoding="utf-8")
        return finished_turn()

    monkeypatch.setattr("inkwell.agent.pipeline.query", capture)
    await rewrite_final(notes, draft_path, [], output_path=output_path)

    assert "Citation diversity" not in tasks[0]


def test_the_report_renders_both_halves_for_the_rewrite_to_read() -> None:
    computed = distribution_report(
        compiled(
            finding(
                *(
                    recorded(f"https://arxiv.org/abs/{n}", domain="arxiv.org")
                    for n in range(3)
                ),
                recorded("https://nature.com/a", domain="nature.com"),
            )
        )
    )
    judged = judged_report(
        [
            JudgedClaim(
                claim=ContestedClaim(
                    scope="Growth", finding=finding(recorded("https://a/b"))
                ),
                verdict=OneSidedVerdict(
                    one_sided=True, missing_side="the stagnation reading"
                ),
            )
        ]
    )
    rendered = merge_reports(computed, judged).render()

    assert "advisory" in rendered
    assert "domain concentration" in rendered
    assert "position diversity" in rendered
    assert "the stagnation reading" in rendered
    assert "1 contested claim(s) judged, 1 found one-sided" in rendered
