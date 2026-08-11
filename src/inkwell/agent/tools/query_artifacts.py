"""Structured query tools for pipeline artifacts.

Loads typed Pydantic models from JSON artifacts and filters them,
so agents don't have to Read raw JSON and parse it manually.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from lup.mcp import LupMcpTool, ToolError, lup_tool

from inkwell.agent.models import (
    ArticlePlan,
    ResearchCompilation,
    ResearchFinding,
    ResearchSource,
    SectionPlan,
)
from inkwell.agent.provenance import UNDATED, EvidentialRole, Venue
from inkwell.agent.tools.citations import BibliographyEntry

logger = logging.getLogger(__name__)

LOOSE_MATCH_OVERLAP = 0.4
"""How much wording a question and a finding must share to count as the same
question, once neither an exact nor a containment match has been found."""


class EmptyInput(BaseModel):
    pass


class FindingSummary(BaseModel):
    question: str
    confidence: float
    source_count: int


class ListResearchOutput(BaseModel):
    total_findings: int
    findings: list[FindingSummary]
    additional_context: str


class ReadFindingInput(BaseModel):
    questions: list[str] = Field(
        description=(
            "Questions to look up (from list_research output). "
            "Matches fuzzily — a substring of the original question works."
        ),
    )


class CitedSource(BaseModel):
    """One source as a later stage needs it in order to weigh it.

    A URL alone cannot say whether the document was peer-reviewed or posted to a
    forum, or whether it is where the claim originated — so what research
    recorded travels with it rather than being guessed at again here.
    """

    url: str = Field(description="Where the source is")
    title: str = Field(description="Source title")
    venue: Venue = Field(
        default="unknown", description="Venue authority as research recorded it"
    )
    published: str = Field(
        default="",
        description=f"Publication date as recorded (ISO 8601), or {UNDATED!r}",
    )
    role: EvidentialRole | None = Field(
        default=None,
        description=(
            "What this source is evidence of for this finding: 'commentary' "
            "relays a claim that originated elsewhere, and attributed_to names "
            "whose — cite the originator's own work for their thesis, or say in "
            "the text whose reading this is. Absent on an older artifact"
        ),
    )
    attributed_to: str = Field(
        default="", description="Whose claim a commentary source relays"
    )


def cited_source(source: ResearchSource) -> CitedSource:
    """One source with the axes research recorded, defaulted where it recorded none."""
    recorded = source.provenance
    return CitedSource(
        url=source.url,
        title=source.title,
        venue=source.venue(),
        published=source.published(),
        role=recorded.role if recorded is not None else None,
        attributed_to=recorded.attributed_to if recorded is not None else "",
    )


class FindingDetail(BaseModel):
    question: str
    answer: str
    confidence: float
    sources: list[CitedSource]
    data_points: list[str]


class ReadFindingOutput(BaseModel):
    findings: list[FindingDetail]


class ListSourcesOutput(BaseModel):
    sources: list[BibliographyEntry]
    total: int
    provenance_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "What the recorded provenance shows the research is missing — a "
            "claim reached only through commentary on it, a finding resting on "
            "background alone. Read these before citing: a claim attributed to "
            "someone whose own work was never read needs the attribution said "
            "out loud in the text"
        ),
    )


class QueryPlanInput(BaseModel):
    section: str = Field(
        default="",
        description="Return details for this section title only. Empty returns all.",
    )


class SectionResult(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    quotes_to_include: list[str]
    research_questions: list[str]


class QueryPlanOutput(BaseModel):
    title: str
    thesis: str
    target_format: str
    sections: list[SectionResult]
    total_research_questions: int


def make_query_tools(artifacts_dir: Path) -> list[LupMcpTool]:
    """Create query tools bound to a pipeline's artifacts directory."""

    def load_research() -> ResearchCompilation | None:
        path = artifacts_dir / "research.json"
        if not path.exists():
            return None
        return ResearchCompilation.model_validate_json(path.read_text(encoding="utf-8"))

    def load_plan() -> ArticlePlan | None:
        path = artifacts_dir / "plan.json"
        if not path.exists():
            return None
        return ArticlePlan.model_validate_json(path.read_text(encoding="utf-8"))

    def find_best_match(
        query: str, research: ResearchCompilation
    ) -> ResearchFinding | None:
        q = query.strip().lower()
        for f in research.findings:
            if f.question.strip().lower() == q:
                return f
        for f in research.findings:
            if q in f.question.lower() or f.question.lower() in q:
                return f
        asked = {*q.split()}

        def word_overlap(finding: ResearchFinding) -> float:
            """How much wording the question and this finding share, 0 to 1."""
            answered = {*finding.question.lower().split()}
            return len(asked & answered) / max(len(asked | answered), 1)

        best = max(research.findings, key=word_overlap, default=None)
        if best is not None and word_overlap(best) > LOOSE_MATCH_OVERLAP:
            return best
        return None

    @lup_tool(
        "List all research findings as a browsable index. Returns each "
        "finding's question, confidence, and source count. Call "
        "read_finding with the question text to get full answers and sources.",
        name="list_research",
    )
    async def list_research(_inp: EmptyInput) -> ListResearchOutput:
        research = load_research()
        if research is None:
            raise ToolError(
                "No research artifact found — research stage hasn't run yet"
            )

        summaries = [
            FindingSummary(
                question=f.question,
                confidence=f.confidence,
                source_count=len(f.sources),
            )
            for f in research.findings
        ]

        return ListResearchOutput(
            total_findings=len(research.findings),
            findings=summaries,
            additional_context=research.additional_context,
        )

    @lup_tool(
        "Read full details for specific research findings by question. Use "
        "list_research first to see all available findings, then call this "
        "with the questions you want. Matches fuzzily — a substring or "
        "slight rewording of the original question works. Returns the "
        "complete answer, extracted data points, and each source with the "
        "venue and evidential role research recorded for it — a source marked "
        "'commentary' relays someone else's claim, so cite the originator's own "
        "work or say in the text whose reading it is.",
        name="read_finding",
    )
    async def read_finding(inp: ReadFindingInput) -> ReadFindingOutput:
        research = load_research()
        if research is None:
            raise ToolError(
                "No research artifact found — research stage hasn't run yet"
            )

        def detail(query: str) -> FindingDetail:
            """The finding answering `query`, or an error naming what exists."""
            match = find_best_match(query, research)
            if match is None:
                available = [f.question for f in research.findings]
                raise ToolError(
                    f"No finding matching {query!r}. Available: {available}"
                )
            return FindingDetail(
                question=match.question,
                answer=match.answer,
                confidence=match.confidence,
                sources=[cited_source(s) for s in match.sources],
                data_points=match.data_points,
            )

        return ReadFindingOutput(findings=[detail(query) for query in inp.questions])

    @lup_tool(
        "Query the article plan for section details, research questions, and "
        "structure. Use this to look up what a section should cover, which "
        "quotes to include, or what the thesis is — without reading the full "
        "plan JSON. Filter by section title to get just one section's details.",
        name="query_plan",
    )
    async def query_plan(inp: QueryPlanInput) -> QueryPlanOutput:
        plan = load_plan()
        if plan is None:
            raise ToolError("No plan artifact found — planning stage hasn't run yet")

        sections = plan.sections
        if inp.section:
            sections = [s for s in sections if inp.section.lower() in s.title.lower()]

        def summarized(section: SectionPlan) -> SectionResult:
            """One planned section, with the questions asked on its behalf."""
            return SectionResult(
                title=section.title,
                summary=section.summary,
                key_points=section.key_points,
                quotes_to_include=section.quotes_to_include,
                research_questions=[
                    q.question
                    for q in plan.research_questions
                    if q.section.lower() == section.title.lower()
                ],
            )

        return QueryPlanOutput(
            title=plan.title,
            thesis=plan.thesis,
            target_format=plan.target_format,
            sections=[summarized(section) for section in sections],
            total_research_questions=len(plan.research_questions),
        )

    @lup_tool(
        "List all unique sources from research findings as citation candidates. "
        "Returns deduplicated sources with title, URL, the venue and publication "
        "date recorded during research, and any gaps that record shows — such as "
        "a claim reached only through commentary on it. Use before "
        "format_bibliography to see what sources are available for building a "
        "reference section, and pass the venue and date through unchanged.",
        name="list_sources",
    )
    async def list_sources(_inp: EmptyInput) -> ListSourcesOutput:
        research = load_research()
        if research is None:
            raise ToolError(
                "No research artifact found — research stage hasn't run yet"
            )

        def candidate(source: ResearchSource) -> BibliographyEntry:
            """One citation candidate, carrying the venue and date research recorded.

            An older artifact recorded neither, which is what the defaults stand
            for — an unknown venue here means nobody derived one, not that the
            derivation came back empty.
            """
            return BibliographyEntry(
                title=source.title,
                url=source.url,
                key_excerpt=source.key_excerpt,
                venue=source.venue(),
                published=source.published(),
            )

        cited = [source for finding in research.findings for source in finding.sources]
        entries = [
            candidate(source)
            for index, source in enumerate(cited)
            if not any(earlier.url == source.url for earlier in cited[:index])
        ]
        return ListSourcesOutput(
            sources=entries,
            total=len(entries),
            provenance_gaps=sorted(
                {
                    gap
                    for finding in research.findings
                    for gap in finding.provenance_gaps()
                }
            ),
        )

    return [list_research, read_finding, query_plan, list_sources]
