"""Structured query tools for pipeline artifacts.

Loads typed Pydantic models from JSON artifacts and filters them,
so agents don't have to Read raw JSON and parse it manually.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from lup.mcp import LupMcpTool, ToolError, lup_tool

from inkwell.agent.models import ArticlePlan, ResearchCompilation

logger = logging.getLogger(__name__)


class QueryResearchInput(BaseModel):
    section: str = Field(
        default="",
        description=(
            "Filter findings to those relevant to this section title. "
            "Matches against the original research question text. "
            "Leave empty to return all findings."
        ),
    )
    claim: str = Field(
        default="",
        description=(
            "Search for findings related to this specific claim or topic. "
            "Matches against question, answer, and data_points. "
            "Leave empty to skip claim filtering."
        ),
    )
    min_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Only return findings with confidence >= this value",
    )


class FindingResult(BaseModel):
    question: str
    answer: str
    confidence: float
    source_count: int
    source_urls: list[str]
    data_points: list[str]


class QueryResearchOutput(BaseModel):
    total_findings: int
    matched_findings: int
    findings: list[FindingResult]
    additional_context: str


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
        return ResearchCompilation.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def load_plan() -> ArticlePlan | None:
        path = artifacts_dir / "plan.json"
        if not path.exists():
            return None
        return ArticlePlan.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def matches_text(query: str, *fields: str) -> bool:
        q = query.lower()
        return any(q in field.lower() for field in fields)

    @lup_tool(
        "Query research findings with optional filters. Use this instead of "
        "reading research.json directly — it loads the structured data, filters "
        "by section, claim text, or confidence, and returns typed results with "
        "source URLs and data points. Call with no filters to get a summary of "
        "all findings.",
        name="query_research",
    )
    async def query_research(inp: QueryResearchInput) -> QueryResearchOutput:
        research = load_research()
        if research is None:
            raise ToolError("No research artifact found — research stage hasn't run yet")

        matched = research.findings

        if inp.section:
            matched = [
                f for f in matched
                if matches_text(inp.section, f.question)
            ]

        if inp.claim:
            matched = [
                f for f in matched
                if matches_text(
                    inp.claim,
                    f.question,
                    f.answer,
                    *f.data_points,
                )
            ]

        if inp.min_confidence > 0:
            matched = [f for f in matched if f.confidence >= inp.min_confidence]

        results = [
            FindingResult(
                question=f.question,
                answer=f.answer,
                confidence=f.confidence,
                source_count=len(f.sources),
                source_urls=[s.url for s in f.sources],
                data_points=f.data_points,
            )
            for f in matched
        ]

        return QueryResearchOutput(
            total_findings=len(research.findings),
            matched_findings=len(results),
            findings=results,
            additional_context=research.additional_context,
        )

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
            sections = [
                s for s in sections
                if inp.section.lower() in s.title.lower()
            ]

        results = []
        for s in sections:
            section_questions = [
                q.question for q in plan.research_questions
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

    return [query_research, query_plan]
