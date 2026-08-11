"""What a recorded research source says about how the run came to have it.

The derivation is what these pin down: a venue read off the acquisition rather
than asserted, a date that is either real or explicitly absent, and an evidential
role that makes citing a commentator for someone else's thesis visible in the
artifact instead of leaving it for a reviewer to catch.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import arxiv
import pytest
from lup.mcp import ToolError, response_text
from lup.workspace.content_safety import SavedContent
from pydantic import ValidationError

from inkwell.agent.models import ResearchCompilation
from inkwell.agent.provenance import (
    UNDATED,
    Acquisition,
    DomainVenue,
    EvidentialRole,
    SourceProvenance,
    Venue,
    VenueRules,
)
from inkwell.agent.tools.citations import BibliographyEntry, do_format_bibliography
from inkwell.agent.tools.query_artifacts import make_query_tools
from inkwell.agent.tools.research.arxiv import FetchArxivOutput, result_to_paper
from inkwell.agent.tools.research.exa import WireResult
from inkwell.agent.tools.research.fetch import (
    FetchAndExtractOutput,
    FetchSourceOutput,
)
from inkwell.agent.tools.research.fred import FredSeriesInfo, FredSeriesOutput
from inkwell.agent.tools.research.wikipedia import FetchWikipediaOutput
from inkwell.agent.tools.source_consult import SourceDocument
from inkwell.agent.tools.stage_outputs import (
    RecordFindingInput,
    ResearchCollector,
    ResearchSourceInput,
    make_research_output_tools,
)


def cited(
    acquisition: Acquisition,
    *,
    title: str = "A document",
    role: EvidentialRole = "primary",
    attributed_to: str = "",
    published: str = "",
    locator: str = "",
    venue_override: Venue | None = None,
    override_reason: str = "",
) -> ResearchSourceInput:
    """One source as the researcher records it, before the venue is derived."""
    return ResearchSourceInput(
        title=title,
        acquisition=acquisition,
        relevance="It bears on the question",
        key_excerpt="The sentence that matters.",
        role=role,
        attributed_to=attributed_to,
        published=published,
        locator=locator,
        venue_override=venue_override,
        override_reason=override_reason,
    )


def provenance_of(source: ResearchSourceInput) -> SourceProvenance:
    """The provenance recording that source derives, which is never absent."""
    recorded = source.recorded().provenance
    assert recorded is not None
    return recorded


def finding(
    *sources: ResearchSourceInput,
    origin: Literal[
        "source_document", "external", "mixed", "author_unverified"
    ] = "external",
) -> RecordFindingInput:
    """A finding citing those sources, as record_finding receives it."""
    return RecordFindingInput(
        question="Whose thesis is the intelligence explosion?",
        answer="I. J. Good stated it in 1965.",
        origin=origin,
        confidence=0.8,
        sources=list(sources),
    )


class TestVenueDerivedFromAcquisition:
    @pytest.mark.parametrize(
        ("acquisition", "venue"),
        [
            (
                Acquisition(path="arxiv", url="https://arxiv.org/abs/2301.12345"),
                "preprint",
            ),
            (
                Acquisition(path="exa_search", url="https://www.nature.com/articles/x"),
                "peer_reviewed",
            ),
            (
                Acquisition(
                    path="exa_search", url="https://www.lesswrong.com/posts/a/b"
                ),
                "forum_post",
            ),
            (
                Acquisition(
                    path="url_fetch", url="https://journals.plos.org/one/a?id=1"
                ),
                "peer_reviewed",
            ),
            (
                Acquisition(path="url_fetch", url="https://www.bls.gov/news.release"),
                "official_report",
            ),
            (
                Acquisition(path="url_fetch", url="https://openai.com/index/gpt-4"),
                "lab_publication",
            ),
            (
                Acquisition(path="url_fetch", url="https://someone.substack.com/p/x"),
                "blog",
            ),
            (Acquisition(path="url_fetch", url="https://example.invalid/x"), "unknown"),
            (
                Acquisition(path="wikipedia", url="https://en.wikipedia.org/wiki/X"),
                "encyclopedia",
            ),
            (
                Acquisition(
                    path="fred", url="https://fred.stlouisfed.org/series/UNRATE"
                ),
                "dataset",
            ),
            (
                Acquisition(
                    path="prediction_market", url="https://polymarket.com/event/x"
                ),
                "market",
            ),
            (
                Acquisition(path="source_document", url="/notes/sources/paper.pdf"),
                "source_document",
            ),
        ],
    )
    def test_each_acquisition_implies_its_venue(
        self, acquisition: Acquisition, venue: Venue
    ) -> None:
        assert acquisition.venue() == venue

    def test_the_path_answers_before_the_host_where_it_settles_the_venue(self) -> None:
        # arXiv serves preprints whichever of its URLs was linked.
        as_pdf = Acquisition(path="arxiv", url="https://arxiv.org/pdf/2301.12345")
        assert as_pdf.venue() == "preprint"

    def test_the_more_specific_domain_answers(self) -> None:
        reports = Acquisition(path="url_fetch", url="https://www.worldbank.org/en/news")
        data = Acquisition(path="url_fetch", url="https://data.worldbank.org/ind/X")
        assert reports.venue() == "official_report"
        assert data.venue() == "dataset"

    def test_the_rules_are_a_caller_s_to_replace(self) -> None:
        internal = VenueRules(
            by_path=[],
            by_host=[DomainVenue(domain="wiki.acme.internal", venue="official_report")],
        )
        acquisition = Acquisition(path="arxiv", url="https://wiki.acme.internal/spec")
        assert acquisition.venue(internal) == "official_report"
        assert acquisition.venue() == "preprint"

    def test_a_forum_post_cannot_be_recorded_as_peer_reviewed(self) -> None:
        recorded = provenance_of(
            cited(
                Acquisition(
                    path="url_fetch", url="https://www.lesswrong.com/posts/a/b"
                ),
                published="2008-11-01",
            )
        )
        assert recorded.venue == "forum_post"
        assert recorded.asserted() is False


class TestToolsCarryTheirAcquisition:
    """Every research tool hands back the record its results were acquired by."""

    def saved(self, tmp_path: Path) -> SavedContent:
        """Stand-in for content a tool wrote to disk."""
        return SavedContent(
            path=str(tmp_path / "c.md"), word_count=2, char_count=9, preview="some text"
        )

    def test_a_fetch_reports_the_url_it_read(self, tmp_path: Path) -> None:
        fetched = FetchSourceOutput(
            url="https://www.lesswrong.com/posts/a/b",
            format="text",
            content=self.saved(tmp_path),
        )
        assert fetched.acquisition.path == "url_fetch"
        assert fetched.acquisition.venue() == "forum_post"
        # The agent has to see it in the result to copy it into record_finding.
        assert fetched.model_dump()["acquisition"]["path"] == "url_fetch"

    def test_a_focused_extract_reports_the_same(self, tmp_path: Path) -> None:
        extracted = FetchAndExtractOutput(
            url="https://www.nature.com/articles/x", extract="…"
        )
        assert extracted.acquisition.venue() == "peer_reviewed"

    def test_an_arxiv_fetch_reports_a_preprint(self) -> None:
        paper = FetchArxivOutput(
            paper_id="2001.08361", format="pdf", url="https://arxiv.org/pdf/2001.08361"
        )
        assert paper.acquisition.path == "arxiv"
        assert paper.acquisition.venue() == "preprint"

    def test_an_arxiv_search_hit_reports_its_publication_date(self) -> None:
        found = result_to_paper(
            arxiv.Result(
                entry_id="https://arxiv.org/abs/2001.08361",
                published=datetime(2020, 1, 23, tzinfo=UTC),
                title="Scaling Laws for Neural Language Models",
            )
        )
        assert found["acquisition"].published == "2020-01-23"
        assert found["acquisition"].venue() == "preprint"

    def test_an_exa_hit_reports_the_date_exa_gave(self) -> None:
        hit = WireResult.model_validate(
            {
                "title": "A study",
                "url": "https://www.nature.com/articles/x",
                "publishedDate": "2024-02-01T00:00:00.000Z",
            }
        )
        assert hit.acquisition().published == "2024-02-01T00:00:00.000"
        assert hit.acquisition().venue() == "peer_reviewed"
        assert provenance_of(cited(hit.acquisition())).year() == "2024"

    def test_a_wikipedia_article_reports_an_encyclopedia(self, tmp_path: Path) -> None:
        article = FetchWikipediaOutput(
            title="Prediction market",
            url="https://en.wikipedia.org/wiki/Prediction_market",
            content=self.saved(tmp_path),
        )
        assert article.acquisition.venue() == "encyclopedia"

    def test_a_fred_series_reports_the_vintage_it_was_read_at(self) -> None:
        series = FredSeriesOutput(
            series_id="UNRATE",
            info=FredSeriesInfo(
                series_id="UNRATE",
                title="Unemployment Rate",
                units="Percent",
                frequency="Monthly",
                seasonal_adjustment="Seasonally Adjusted",
                last_updated="2024-03-14 07:31:02-05",
            ),
            observations=[],
            count=0,
        )
        assert series.acquisition.venue() == "dataset"
        assert provenance_of(cited(series.acquisition)).year() == "2024"

    def test_the_author_s_own_document_reports_itself(self) -> None:
        document = SourceDocument(
            label="paper", path="/notes/sources/paper.pdf", kind="pdf", page_count=200
        )
        assert document.acquisition.path == "source_document"
        assert document.acquisition.venue() == "source_document"


class TestVenueOverride:
    def test_an_override_records_its_reason(self) -> None:
        recorded = provenance_of(
            cited(
                Acquisition(path="url_fetch", url="https://cs.example.edu/~k/p.pdf"),
                published="2019",
                venue_override="peer_reviewed",
                override_reason="Author's copy of the CHI 2019 proceedings paper.",
            )
        )
        assert recorded.venue == "peer_reviewed"
        assert recorded.asserted() is True
        assert "CHI 2019" in recorded.venue_reason

    def test_an_override_without_a_reason_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="override_reason"):
            cited(
                Acquisition(path="url_fetch", url="https://forum.example.com/t/1"),
                published="2019",
                venue_override="peer_reviewed",
            )


class TestPublicationDate:
    def test_the_acquisition_supplies_the_date_when_it_has_one(self) -> None:
        recorded = provenance_of(
            cited(
                Acquisition(
                    path="arxiv",
                    url="https://arxiv.org/abs/2301.12345",
                    published="2023-01-30",
                )
            )
        )
        assert recorded.published == "2023-01-30"
        assert recorded.year() == "2023"
        assert recorded.undated() is False

    def test_the_agent_supplies_the_date_when_the_acquisition_cannot(self) -> None:
        recorded = provenance_of(
            cited(
                Acquisition(path="url_fetch", url="https://example.invalid/essay"),
                published="1965-04",
            )
        )
        assert recorded.year() == "1965"

    def test_an_undated_source_says_so_explicitly(self) -> None:
        recorded = provenance_of(
            cited(
                Acquisition(path="url_fetch", url="https://example.invalid/x"),
                published=UNDATED,
            )
        )
        assert recorded.undated() is True
        assert recorded.year() == ""

    def test_a_blank_date_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="undated"):
            cited(Acquisition(path="url_fetch", url="https://example.invalid/x"))

    def test_a_date_no_check_can_read_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="ISO 8601"):
            cited(
                Acquisition(path="url_fetch", url="https://example.invalid/x"),
                published="last spring",
            )


class TestEvidentialRole:
    def test_commentary_must_name_whose_claim_it_relays(self) -> None:
        with pytest.raises(ValidationError, match="attributed_to"):
            cited(
                Acquisition(
                    path="url_fetch", url="https://www.lesswrong.com/posts/a/b"
                ),
                role="commentary",
                published="2008",
            )

    def test_a_claim_reached_only_through_commentary_is_visible(self) -> None:
        relayed = cited(
            Acquisition(path="url_fetch", url="https://www.lesswrong.com/posts/a/b"),
            title="Artificial Intelligence as a Positive and Negative Factor",
            role="commentary",
            attributed_to="I. J. Good",
            published="2008",
        )
        gaps = finding(relayed).recorded().provenance_gaps()
        assert len(gaps) == 1
        assert "I. J. Good" in gaps[0]

    def test_the_primary_source_closes_the_gap(self) -> None:
        relayed = cited(
            Acquisition(path="url_fetch", url="https://www.lesswrong.com/posts/a/b"),
            role="commentary",
            attributed_to="I. J. Good",
            published="2008",
        )
        original = cited(
            Acquisition(path="url_fetch", url="https://www.sciencedirect.com/g1965"),
            title="Speculations Concerning the First Ultraintelligent Machine",
            published="1965",
        )
        assert finding(relayed, original).recorded().provenance_gaps() == []

    def test_background_alone_is_not_evidence(self) -> None:
        context = cited(
            Acquisition(path="wikipedia", url="https://en.wikipedia.org/wiki/X"),
            role="background",
            published=UNDATED,
        )
        assert finding(context).recorded().provenance_gaps() == [
            "Every source is background — nothing recorded is evidence "
            "for the answer itself."
        ]

    def test_a_source_document_citation_must_say_where_in_it(self) -> None:
        with pytest.raises(ValidationError, match="locator"):
            cited(
                Acquisition(path="source_document", url="/notes/sources/paper.pdf"),
                published="2021",
            )


class TestRecordFindingTool:
    async def test_the_gaps_come_back_to_the_researcher(self, tmp_path: Path) -> None:
        collector = ResearchCollector(tmp_path / "research.json")
        record = next(
            t
            for t in make_research_output_tools(collector)
            if t.name == "record_finding"
        )
        relayed = cited(
            Acquisition(path="url_fetch", url="https://www.lesswrong.com/posts/a/b"),
            role="commentary",
            attributed_to="I. J. Good",
            published="2008",
        )

        answered = response_text(await record.handler(finding(relayed).model_dump()))

        assert '"ok": true' in answered
        assert "I. J. Good" in answered
        recorded = collector.research.findings[0].recorded_provenance()
        assert [p.venue for p in recorded] == ["forum_post"]
        assert [p.role for p in recorded] == ["commentary"]

    async def test_a_source_document_origin_needs_the_document(
        self, tmp_path: Path
    ) -> None:
        collector = ResearchCollector(tmp_path / "research.json")
        record = next(
            t
            for t in make_research_output_tools(collector)
            if t.name == "record_finding"
        )
        external = cited(
            Acquisition(path="arxiv", url="https://arxiv.org/abs/2301.1"),
            published="2023-01-30",
        )

        with pytest.raises(ToolError, match="source_document"):
            await record.call_handler(finding(external, origin="source_document"))

        assert collector.research.findings == []

    async def test_a_document_citation_records_the_author_s_own_material(
        self, tmp_path: Path
    ) -> None:
        collector = ResearchCollector(tmp_path / "research.json")
        record = next(
            t
            for t in make_research_output_tools(collector)
            if t.name == "record_finding"
        )
        from_document = cited(
            Acquisition(path="source_document", url="/notes/sources/paper.pdf"),
            published="2021",
            locator="p. 142",
        )

        await record.call_handler(finding(from_document, origin="source_document"))

        recorded = collector.research.findings[0].recorded_provenance()[0]
        assert recorded.venue == "source_document"
        assert recorded.acquired_via == "source_document"


OLDER_ARTIFACT = {
    "findings": [
        {
            "question": "What share of papers are preprints?",
            "answer": "About a third.",
            "origin": "external",
            "confidence": 0.6,
            "sources": [
                {
                    "title": "A study",
                    "url": "https://www.nature.com/articles/x",
                    "relevance": "measures it",
                    "key_excerpt": "roughly a third",
                }
            ],
            "data_points": [],
        }
    ],
    "additional_context": "",
    "suggested_additions": [],
}
"""A research artifact as it was written before provenance was recorded."""


class TestOlderArtifacts:
    def test_an_artifact_without_provenance_still_validates(self) -> None:
        research = ResearchCompilation.model_validate(OLDER_ARTIFACT)
        # Absent, not undated: nobody derived a venue or asked for a date.
        assert research.findings[0].sources[0].provenance is None
        assert research.findings[0].recorded_provenance() == []
        assert research.findings[0].provenance_gaps() == []

    async def test_list_sources_reads_an_older_artifact(self, tmp_path: Path) -> None:
        (tmp_path / "research.json").write_text(
            json.dumps(OLDER_ARTIFACT), encoding="utf-8"
        )
        list_sources = next(
            t for t in make_query_tools(tmp_path) if t.name == "list_sources"
        )

        listed = json.loads(response_text(await list_sources.handler({})))

        assert listed["sources"][0]["venue"] == "unknown"
        assert listed["sources"][0]["published"] == ""

    async def test_read_finding_reads_an_older_artifact(self, tmp_path: Path) -> None:
        (tmp_path / "research.json").write_text(
            json.dumps(OLDER_ARTIFACT), encoding="utf-8"
        )
        read = next(t for t in make_query_tools(tmp_path) if t.name == "read_finding")

        detail = json.loads(
            response_text(await read.handler({"questions": ["preprints"]}))
        )

        # No role was recorded, and nothing invents one.
        assert detail["findings"][0]["sources"][0]["role"] is None
        assert detail["findings"][0]["sources"][0]["venue"] == "unknown"


class TestWritersSeeTheAxes:
    async def test_read_finding_carries_the_venue_and_the_role(
        self, tmp_path: Path
    ) -> None:
        collector = ResearchCollector(tmp_path / "research.json")
        record = next(
            t
            for t in make_research_output_tools(collector)
            if t.name == "record_finding"
        )
        await record.call_handler(
            finding(
                cited(
                    Acquisition(
                        path="url_fetch", url="https://www.lesswrong.com/posts/a/b"
                    ),
                    title="A post about someone else's thesis",
                    role="commentary",
                    attributed_to="I. J. Good",
                    published="2008",
                )
            )
        )
        read = next(t for t in make_query_tools(tmp_path) if t.name == "read_finding")

        detail = json.loads(
            response_text(await read.handler({"questions": ["intelligence explosion"]}))
        )
        source = detail["findings"][0]["sources"][0]

        assert source["venue"] == "forum_post"
        assert source["role"] == "commentary"
        assert source["attributed_to"] == "I. J. Good"


class TestBibliographyReadsTheVenue:
    def test_the_venue_tags_the_entry(self) -> None:
        output = do_format_bibliography(
            "numbered",
            [
                BibliographyEntry(
                    title="Scaling Laws",
                    url="https://arxiv.org/abs/2001.08361",
                    authors=["Jared Kaplan"],
                    venue="preprint",
                    published="2020-01-23",
                ),
                BibliographyEntry(
                    title="A Post",
                    url="https://www.lesswrong.com/posts/a/b",
                    venue="forum_post",
                    published=UNDATED,
                ),
                BibliographyEntry(
                    title="A Paper",
                    url="https://www.nature.com/articles/x",
                    venue="peer_reviewed",
                    published="2024",
                ),
            ],
        )
        assert '(2020) "Scaling Laws" [preprint]' in output.bibliography
        assert '"A Post" [forum post]' in output.bibliography
        # A journal paper carries no tag — that is what a reference list assumes.
        assert '"A Paper"' in output.bibliography
        assert "peer reviewed" not in output.bibliography

    def test_an_undated_entry_shows_no_year(self) -> None:
        output = do_format_bibliography(
            "author-date",
            [
                BibliographyEntry(
                    title="A Post",
                    url="https://www.lesswrong.com/posts/a/b",
                    authors=["Eliezer Yudkowsky"],
                    venue="forum_post",
                    published=UNDATED,
                )
            ],
        )
        assert output.citation_map["https://www.lesswrong.com/posts/a/b"] == (
            "(Yudkowsky)"
        )

    def test_the_recorded_date_becomes_the_citation_year(self) -> None:
        output = do_format_bibliography(
            "author-date",
            [
                BibliographyEntry(
                    title="Speculations",
                    url="https://example.invalid/good1965",
                    authors=["Irving John Good"],
                    venue="peer_reviewed",
                    published="1965-04",
                )
            ],
        )
        assert output.citation_map["https://example.invalid/good1965"] == "(Good, 1965)"

    async def test_the_bibliography_runs_on_what_research_recorded(
        self, tmp_path: Path
    ) -> None:
        collector = ResearchCollector(tmp_path / "research.json")
        record = next(
            t
            for t in make_research_output_tools(collector)
            if t.name == "record_finding"
        )
        await record.call_handler(
            finding(
                cited(
                    Acquisition(
                        path="arxiv",
                        url="https://arxiv.org/abs/2001.08361",
                        published="2020-01-23",
                    ),
                    title="Scaling Laws",
                )
            )
        )
        list_sources = next(
            t for t in make_query_tools(tmp_path) if t.name == "list_sources"
        )

        listed = json.loads(response_text(await list_sources.handler({})))
        entries = [
            BibliographyEntry.model_validate(entry) for entry in listed["sources"]
        ]

        assert [entry.venue for entry in entries] == ["preprint"]
        rendered = do_format_bibliography("numbered", entries)
        assert "(2020)" in rendered.bibliography
        assert "[preprint]" in rendered.bibliography
