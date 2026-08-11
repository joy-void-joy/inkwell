"""What the citation checks say about a citation set, and what they never do.

Two properties carry most of the weight. The distribution is read off the fields
research recorded, so a section whose sources carry none reports as unknown rather
than as a handful of distinct values that look like spread — the failure a check
counting URLs would make. And both checks are advisory: a leaning citation set, a
one-sided claim, and a judge that dies mid-verdict all leave the run going, with
the rows reaching the stage that rewrites the prose and the author's document.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from lup.runtime.models import SessionId, TurnId, TurnIdentifiers, TurnResult
from lup.types import Usage
from pydantic import ValidationError

from inkwell.agent.diversity import (
    DEFAULT_DIVERSITY_RULES,
    WHOLE_WORK,
    AuthorAxis,
    ContestedClaim,
    ContestedSignal,
    DiversityRules,
    DomainAxis,
    JudgedClaim,
    OneSidedVerdict,
    OrganizationAxis,
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


def finished_turn() -> TurnResult[None]:
    """A stage agent that ran and returned no text of its own."""
    return TurnResult(
        output=None,
        messages=[],
        blocks=[],
        usage=Usage(),
        duration=timedelta(),
        identifiers=TurnIdentifiers(
            session=SessionId(value="session"), turn=TurnId(value="turn")
        ),
    )


@pytest.fixture
def notes(tmp_path: Path) -> PipelineNotes:
    return PipelineNotes(tmp_path / "notes")


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
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row at once, and the check still returns a report."""

    async def one_sided(*_args: object, **_kwargs: object) -> OneSidedVerdict:
        return OneSidedVerdict(
            one_sided=True, missing_side="the other camp", reason="All one way."
        )

    monkeypatch.setattr("inkwell.agent.pipeline.query", one_sided)
    research = compiled(
        finding(
            *(unrecorded(f"https://arxiv.org/abs/{n}") for n in range(4)),
            confidence=0.2,
        )
    )

    report = await check_citation_diversity(notes, research)

    assert report.rows
    assert report.judged == 1
    assert report.author_notes()


async def test_a_judge_that_dies_costs_its_row_and_nothing_more(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def collapse(*_args: object, **_kwargs: object) -> OneSidedVerdict:
        raise RuntimeError("Agent error: You've hit your limit")

    monkeypatch.setattr("inkwell.agent.pipeline.query", collapse)
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

    report = await check_citation_diversity(notes, research)

    assert "domain concentration" in {row.check for row in report.rows}
    assert "position diversity" not in {row.check for row in report.rows}
    assert report.author_notes() == []


async def test_a_judge_reaching_no_verdict_leaves_the_computed_rows(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_verdict(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("inkwell.agent.pipeline.query", no_verdict)
    research = compiled(finding(unrecorded("https://a/b"), confidence=0.2))

    report = await check_citation_diversity(notes, research)

    assert report.rows
    assert report.judged == 0


async def test_the_judged_pass_is_what_a_cheap_run_turns_off(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distribution costs only the counting, so it runs either way."""

    async def unreachable(*_args: object, **_kwargs: object) -> OneSidedVerdict:
        raise AssertionError("no judge should be spent")

    monkeypatch.setattr("inkwell.agent.pipeline.query", unreachable)
    research = compiled(finding(unrecorded("https://a/b"), confidence=0.1))

    report = await check_citation_diversity(notes, research, judge=False)

    assert report.judged == 0
    assert report.rows


# ---------------------------------------------------------------------------
# The rows reach the stage that rewrites the prose
# ---------------------------------------------------------------------------


async def test_the_rewrite_stage_reads_the_rows(
    notes: PipelineNotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def skip_judging(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("inkwell.agent.pipeline.query", skip_judging)
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
