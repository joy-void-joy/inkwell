"""Structured query tools for pipeline artifacts.

Loads typed Pydantic models from JSON artifacts and filters them,
so agents don't have to Read raw JSON and parse it manually.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from lup.mcp import LupMcpTool, ToolError, lup_tool

from inkwell.agent.models import ArticlePlan, ResearchCompilation, ResearchFinding
from inkwell.agent.tools.citations import BibliographyEntry

logger = logging.getLogger(__name__)


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


class FindingDetail(BaseModel):
    question: str
    answer: str
    confidence: float
    source_urls: list[str]
    data_points: list[str]


class ReadFindingOutput(BaseModel):
    findings: list[FindingDetail]


class ListSourcesOutput(BaseModel):
    sources: list[BibliographyEntry]
    total: int


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
        q_words = set(q.split())  # claude: ignore
        best, best_overlap = None, 0.0
        for f in research.findings:
            f_words = set(f.question.lower().split())  # claude: ignore
            overlap = len(q_words & f_words) / max(len(q_words | f_words), 1)
            if overlap > best_overlap:
                best, best_overlap = f, overlap
        if best_overlap > 0.4:
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
        "complete answer, source URLs, and extracted data points.",
        name="read_finding",
    )
    async def read_finding(inp: ReadFindingInput) -> ReadFindingOutput:
        research = load_research()
        if research is None:
            raise ToolError(
                "No research artifact found — research stage hasn't run yet"
            )

        results: list[FindingDetail] = []
        for query in inp.questions:
            match = find_best_match(query, research)
            if match is None:
                available = [f.question for f in research.findings]
                raise ToolError(
                    f"No finding matching {query!r}. Available: {available}"
                )
            results.append(
                FindingDetail(
                    question=match.question,
                    answer=match.answer,
                    confidence=match.confidence,
                    source_urls=[s.url for s in match.sources],
                    data_points=match.data_points,
                )
            )

        return ReadFindingOutput(findings=results)

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

        results = []
        for s in sections:
            section_questions = [
                q.question
                for q in plan.research_questions
                if q.section.lower() == s.title.lower()
            ]
            results.append(
                SectionResult(
                    title=s.title,
                    summary=s.summary,
                    key_points=s.key_points,
                    quotes_to_include=s.quotes_to_include,
                    research_questions=section_questions,
                )
            )

        return QueryPlanOutput(
            title=plan.title,
            thesis=plan.thesis,
            target_format=plan.target_format,
            sections=results,
            total_research_questions=len(plan.research_questions),
        )

    @lup_tool(
        "List all unique sources from research findings as citation candidates. "
        "Returns deduplicated sources with title, URL, and any available metadata. "
        "Use before format_bibliography to see what sources are available for "
        "building a reference section.",
        name="list_sources",
    )
    async def list_sources(_inp: EmptyInput) -> ListSourcesOutput:
        research = load_research()
        if research is None:
            raise ToolError(
                "No research artifact found — research stage hasn't run yet"
            )

        seen_urls: set[str] = set()
        entries: list[BibliographyEntry] = []
        for finding in research.findings:
            for src in finding.sources:
                if src.url in seen_urls:
                    continue
                seen_urls.add(src.url)
                entries.append(
                    BibliographyEntry(
                        title=src.title,
                        url=src.url,
                        key_excerpt=src.key_excerpt,
                    )
                )

        return ListSourcesOutput(sources=entries, total=len(entries))

    return [list_research, read_finding, query_plan, list_sources]
