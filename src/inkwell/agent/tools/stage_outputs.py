"""Incremental output tools for pipeline stages.

Each structured stage (planner, researcher, reviewer, assumptions) gets
MCP tools that record output piece by piece. Collectors accumulate the
calls and persist to JSON files. The pipeline reads and validates after
the stage completes.

Writing stages (section writer, coherence editor, rewriter) use built-in
Write/Edit tools instead.
"""

import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, Literal, cast

from claude_agent_sdk import SdkMcpTool
from pydantic import BaseModel, Field, ValidationError

from lup.mcp import LupMcpTool, ToolError, ToolResponse, mcp_response
from lup.metrics import collector as metrics_collector

logger = logging.getLogger(__name__)


def build_stage_tool(
    tool_name: str,
    description: str,
    input_model: type[BaseModel],
    handler: Callable[..., Awaitable[BaseModel]],
) -> LupMcpTool:
    """Construct an MCP tool from a handler closure."""

    async def wrapper(args: dict[str, object]) -> ToolResponse:
        start = time.perf_counter()
        is_error = False
        try:
            try:
                params = input_model.model_validate(args)
            except ValidationError as e:
                is_error = True
                return mcp_response(f"Invalid input: {e}", is_error=True)
            try:
                result = await handler(params)
            except ToolError as e:
                is_error = True
                return mcp_response(str(e), is_error=True)
            return mcp_response(json.dumps(result.model_dump(), default=str))
        except Exception:
            is_error = True
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            metrics_collector.record(tool_name, duration_ms, is_error)

    sdk = SdkMcpTool(
        name=tool_name,
        description=description,
        input_schema=input_model.model_json_schema(),
        handler=cast(
            Callable[[dict[str, object]], Awaitable[dict[str, object]]], wrapper
        ),
    )
    return LupMcpTool(
        sdk_tool=sdk,
        input_model=input_model,
        output_model=BaseModel,
    )


class ToolOk(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# Plan collector + tools
# ---------------------------------------------------------------------------


class SetPlanHeaderInput(BaseModel):
    title: str = Field(description="Working title for the article")
    thesis: str = Field(description="Core argument or insight in one sentence")
    target_format: str = Field(
        description="Output format: academic, lesswrong, twitter, blog, dialog, memo, or custom:<description>"
    )
    author_direction: str = Field(
        description="General direction, constraints, or preferences from the author"
    )
    deliverables: list[str] = Field(
        default_factory=list,
        description=(
            "The author's contract: concrete requested outputs in their own "
            "terms, copied from the brief. Do not widen, narrow, or reword "
            "the author's scope."
        ),
    )
    conventions: list[str] = Field(
        default_factory=list,
        description=(
            "Shared conventions every section must follow: recurring terms "
            "and their meanings, names for key concepts, notational or "
            "formatting choices (one item each). Writers working in "
            "parallel inherit these; without them each writer invents "
            "its own."
        ),
    )
    voice_notes: str = Field(
        description="Observations about the author's tone, style, and voice"
    )


class AddSectionInput(BaseModel):
    title: str = Field(description="Section heading")
    summary: str = Field(description="What this section should cover (2-3 sentences)")
    key_points: list[str] = Field(description="Specific points to make")
    quotes_to_include: list[str] = Field(
        default_factory=list,
        description="Source quote texts to weave into this section",
    )


class AddResearchQuestionInput(BaseModel):
    question: str = Field(description="What to research")
    section: str = Field(description="Which section this supports")
    priority: int = Field(
        default=1, ge=1, le=3, description="1=critical, 2=important, 3=nice-to-have"
    )


class AddSourceQuoteInput(BaseModel):
    text: str = Field(description="The exact quote text")
    speaker: str = Field(description="Who said it")
    context: str = Field(description="Why this quote matters for the article")


class PlanCollector:
    """Accumulates incremental plan tool calls. Persists to JSON after each call."""

    def __init__(
        self,
        output_path: Path,
        on_save: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.output_path = output_path
        self.on_save = on_save
        self.header: dict[str, str] = {}
        self.sections: list[dict[str, object]] = []
        self.research_questions: list[dict[str, object]] = []
        self.source_quotes: list[dict[str, str]] = []

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            **self.header,
            "sections": self.sections,
            "research_questions": self.research_questions,
            "source_quotes": self.source_quotes,
        }
        self.output_path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    async def save_and_notify(self) -> None:
        self.save()
        if self.on_save is not None:
            try:
                await self.on_save()
            except (OSError, RuntimeError):
                logger.warning("PlanCollector on_save failed", exc_info=True)


def make_plan_tools(collector: PlanCollector) -> list[LupMcpTool]:
    """Create MCP tools for incremental plan building."""

    async def handle_header(inp: SetPlanHeaderInput) -> ToolOk:
        collector.header = inp.model_dump()
        await collector.save_and_notify()
        return ToolOk()

    async def handle_section(inp: AddSectionInput) -> ToolOk:
        collector.sections.append(inp.model_dump())
        await collector.save_and_notify()
        return ToolOk()

    async def handle_question(inp: AddResearchQuestionInput) -> ToolOk:
        collector.research_questions.append(inp.model_dump())
        await collector.save_and_notify()
        return ToolOk()

    async def handle_quote(inp: AddSourceQuoteInput) -> ToolOk:
        collector.source_quotes.append(inp.model_dump())
        await collector.save_and_notify()
        return ToolOk()

    return [
        build_stage_tool(
            "set_plan_header",
            (
                "Set the article plan header: title, thesis, target format, "
                "author direction, and voice notes. Call this once before "
                "adding sections and questions."
            ),
            SetPlanHeaderInput,
            handle_header,
        ),
        build_stage_tool(
            "add_section",
            (
                "Add a section to the article plan. Call once per section, "
                "in order. Each section has a title, summary, key points, "
                "and optional quotes to weave in."
            ),
            AddSectionInput,
            handle_section,
        ),
        build_stage_tool(
            "add_research_question",
            (
                "Add a research question to investigate. Specify which "
                "section it supports and priority (1=critical, 2=important, "
                "3=nice-to-have). Be thorough — these drive the research stage."
            ),
            AddResearchQuestionInput,
            handle_question,
        ),
        build_stage_tool(
            "add_source_quote",
            (
                "Preserve an exact quote from the source material. Include "
                "the speaker and why this quote matters for the article."
            ),
            AddSourceQuoteInput,
            handle_quote,
        ),
    ]


# ---------------------------------------------------------------------------
# Research collector + tools
# ---------------------------------------------------------------------------


class ResearchSourceInput(BaseModel):
    title: str = Field(description="Source title")
    url: str = Field(description="Source URL, or file path for source documents")
    relevance: str = Field(description="Why this source matters")
    key_excerpt: str = Field(description="Most relevant excerpt (exact quote)")
    locator: str = Field(
        default="",
        description=(
            "Where the excerpt lives: page number(s) for documents "
            "('p. 142'), chapter/section, or URL fragment. Required when "
            "the source is the author's source document."
        ),
    )


class RecordFindingInput(BaseModel):
    question: str = Field(description="The original research question")
    answer: str = Field(description="Synthesized answer based on sources")
    origin: Literal["source_document", "external", "mixed", "author_unverified"] = (
        Field(
            default="external",
            description=(
                "'source_document' = answered from the author's own source "
                "material (every claim needs a verbatim quote + locator); "
                "'external' = answered from other works; 'mixed' = both; "
                "'author_unverified' = a specific the author asserts that you "
                "could neither confirm nor refute — preserve it verbatim and "
                "flag it for the author, do not drop it. "
                "Never record a claim about what the source document says "
                "based on external works."
            ),
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence (0-1)")
    sources: list[ResearchSourceInput] = Field(description="Sources consulted")
    data_points: list[str] = Field(
        default_factory=list,
        description="Specific numbers, dates, or facts found",
    )


class SuggestAdditionInput(BaseModel):
    suggestion: str = Field(
        description="A suggestion for the article that emerged from research"
    )


class SetAdditionalContextInput(BaseModel):
    context: str = Field(
        description="Background context that doesn't fit a specific question"
    )


class ResearchCollector:
    """Accumulates research findings. Persists after each call."""

    def __init__(
        self,
        output_path: Path,
        on_save: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.output_path = output_path
        self.on_save = on_save
        self.findings: list[dict[str, object]] = []
        self.suggested_additions: list[str] = []
        self.additional_context: str = ""

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "findings": self.findings,
            "additional_context": self.additional_context,
            "suggested_additions": self.suggested_additions,
        }
        self.output_path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    async def save_and_notify(self) -> None:
        self.save()
        if self.on_save is not None:
            try:
                await self.on_save()
            except (OSError, RuntimeError):
                logger.warning("ResearchCollector on_save failed", exc_info=True)


def make_research_output_tools(collector: ResearchCollector) -> list[LupMcpTool]:
    """MCP tools for the researcher to record findings incrementally."""

    async def handle_finding(inp: RecordFindingInput) -> ToolOk:
        collector.findings.append(inp.model_dump())
        await collector.save_and_notify()
        return ToolOk()

    async def handle_suggestion(inp: SuggestAdditionInput) -> ToolOk:
        collector.suggested_additions.append(inp.suggestion)
        await collector.save_and_notify()
        return ToolOk()

    async def handle_context(inp: SetAdditionalContextInput) -> ToolOk:
        collector.additional_context = inp.context
        await collector.save_and_notify()
        return ToolOk()

    return [
        build_stage_tool(
            "record_finding",
            (
                "Record a research finding for one question. Call once per "
                "research question after investigating it. Include all "
                "sources with URLs and key excerpts, your synthesized answer, "
                "and confidence level. Set origin honestly: claims about the "
                "author's source document require origin='source_document' "
                "with a verbatim quote and locator (page/section) from that "
                "document — an external paper's convention is not evidence "
                "of what the source document does. When the author asserts a "
                "specific you can neither confirm nor refute, record it with "
                "origin='author_unverified' (preserving the specific in the "
                "answer) instead of leaving it out — that keeps the author's "
                "voice from being silently stripped."
            ),
            RecordFindingInput,
            handle_finding,
        ),
        build_stage_tool(
            "suggest_addition",
            (
                "Suggest something for the article that emerged from research "
                "but wasn't in the original questions. Only use for genuinely "
                "valuable additions, not padding."
            ),
            SuggestAdditionInput,
            handle_suggestion,
        ),
        build_stage_tool(
            "set_additional_context",
            (
                "Record background context that doesn't fit a specific "
                "research question but provides useful framing for the article."
            ),
            SetAdditionalContextInput,
            handle_context,
        ),
    ]


# ---------------------------------------------------------------------------
# Review collector + tools
# ---------------------------------------------------------------------------


class RecordReviewFindingInput(BaseModel):
    severity: Literal["critical", "suggestion", "praise"] = Field(
        description="critical (must fix), suggestion (would improve), praise (works well)"
    )
    location: str = Field(description="Section or paragraph reference")
    issue: str = Field(description="What you found")
    suggestion: str = Field(default="", description="Suggested fix or improvement")
    text_excerpt: str = Field(
        default="",
        description="Exact verbatim text from the draft this refers to",
    )


class ReviewCollector:
    """Accumulates review findings for one reviewer."""

    def __init__(
        self,
        output_path: Path,
        reviewer: str,
        on_save: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.output_path = output_path
        self.reviewer = reviewer
        self.on_save = on_save
        self.findings: list[dict[str, object]] = []

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"findings": self.findings}
        self.output_path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    async def save_and_notify(self) -> None:
        self.save()
        if self.on_save is not None:
            try:
                await self.on_save()
            except (OSError, RuntimeError):
                logger.warning("ReviewCollector on_save failed", exc_info=True)


def make_review_output_tools(collector: ReviewCollector) -> list[LupMcpTool]:
    """MCP tools for a reviewer to record findings incrementally."""

    async def handle_finding(inp: RecordReviewFindingInput) -> ToolOk:
        finding = inp.model_dump()
        finding["reviewer"] = collector.reviewer
        collector.findings.append(finding)
        await collector.save_and_notify()
        return ToolOk()

    return [
        build_stage_tool(
            "record_finding",
            (
                "Record a review finding. Use severity='critical' for issues "
                "that must be fixed, 'suggestion' for improvements, 'praise' "
                "for passages that work well. Always include text_excerpt — "
                "quote the exact passage from the draft verbatim."
            ),
            RecordReviewFindingInput,
            handle_finding,
        ),
    ]


# ---------------------------------------------------------------------------
# Assumptions collector + tools
# ---------------------------------------------------------------------------


class RecordAssumptionInput(BaseModel):
    tag: Literal["direction_check", "assumption", "question", "confusion"] = Field(
        description=(
            "direction_check: the plan could go different ways. "
            "assumption: something taken for granted. "
            "question: a gap research couldn't fill. "
            "confusion: contradictory or unclear info."
        )
    )
    content: str = Field(description="The uncertainty, question, or assumption")
    best_guess: str = Field(
        description="What you'll proceed with if the author doesn't respond"
    )
    anchor_section: str = Field(description="Which plan section this relates to")


class AssumptionsCollector:
    """Accumulates surfaced assumptions."""

    def __init__(
        self,
        output_path: Path,
        on_save: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.output_path = output_path
        self.on_save = on_save
        self.items: list[dict[str, str]] = []

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"items": self.items}
        self.output_path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    async def save_and_notify(self) -> None:
        self.save()
        if self.on_save is not None:
            try:
                await self.on_save()
            except (OSError, RuntimeError):
                logger.warning("AssumptionsCollector on_save failed", exc_info=True)


def make_assumptions_tools(collector: AssumptionsCollector) -> list[LupMcpTool]:
    """MCP tools for surfacing assumptions incrementally."""

    async def handle_assumption(inp: RecordAssumptionInput) -> ToolOk:
        collector.items.append(inp.model_dump())
        await collector.save_and_notify()
        return ToolOk()

    return [
        build_stage_tool(
            "record_assumption",
            (
                "Record an assumption, uncertainty, or question about the "
                "article plan. The pipeline will post this as a Google Doc "
                "comment for the author. Include your best guess — what "
                "you'll proceed with if the author doesn't respond."
            ),
            RecordAssumptionInput,
            handle_assumption,
        ),
    ]


# ---------------------------------------------------------------------------
# Note for author (migrated from drafts.py)
# ---------------------------------------------------------------------------


class AuthorNote(BaseModel):
    """A note from any pipeline stage addressed to the author."""

    note: str = Field(description="The question, flag, or suggestion")
    anchor: str = Field(
        default="",
        description="Section title, quote, or text this note refers to",
    )
    stage: str = Field(default="", description="Pipeline stage that produced this note")


class NoteForAuthorInput(BaseModel):
    note: str = Field(description="A question or flag for the author")
    anchor: str = Field(
        default="",
        description=(
            "Section title, quote, or passage this note refers to. "
            "Helps the author find the relevant context."
        ),
    )


def make_note_tool(notes_collector: list[AuthorNote], stage: str) -> LupMcpTool:
    """Create the note_for_author tool bound to a mutable list."""

    async def handle_note(inp: NoteForAuthorInput) -> ToolOk:
        notes_collector.append(
            AuthorNote(note=inp.note, anchor=inp.anchor, stage=stage)
        )
        logger.info("[%s] Author note: %s", stage, inp.note[:80])
        return ToolOk()

    return build_stage_tool(
        "note_for_author",
        (
            "Flag a question or note for the author. Use this when you need "
            "clarification, want to highlight an assumption, or have a "
            "suggestion that requires the author's input. Set 'anchor' to "
            "the section title or specific text this note refers to."
        ),
        NoteForAuthorInput,
        handle_note,
    )


# ---------------------------------------------------------------------------
# Shared glossary (cross-writer term ledger)
# ---------------------------------------------------------------------------


class GlossaryEntry(BaseModel):
    """One shared term: a name, symbol, or abbreviation and the single meaning
    every section must use for it."""

    term: str = Field(description="The term, name, symbol, or abbreviation")
    meaning: str = Field(description="Its canonical meaning/usage for this piece")


class DefineTermResult(BaseModel):
    """Result of define_term — the canonical entry the caller should conform to."""

    term: str
    meaning: str
    already_defined: bool = Field(
        description=(
            "True when a sibling writer already defined this term; the returned "
            "meaning is the canonical one — conform to it rather than your own."
        )
    )


class DefineTermInput(BaseModel):
    term: str = Field(
        description="The term, name, symbol, or abbreviation you are introducing"
    )
    meaning: str = Field(description="Its definition — how every section should use it")


class LookupTermsInput(BaseModel):
    contains: str = Field(
        default="",
        description=(
            "Optional substring filter over terms; empty returns the whole "
            "shared glossary."
        ),
    )


class GlossaryView(BaseModel):
    conventions: list[str] = Field(
        description="Plan-level conventions seeded before writing"
    )
    terms: list[GlossaryEntry] = Field(
        description="Terms coined by section writers so far"
    )


def load_glossary(path: Path) -> tuple[list[str], list[GlossaryEntry]]:
    """Read the shared glossary file: (seeded conventions, coined terms)."""
    if not path.exists():
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    conventions = [str(c) for c in data.get("conventions", [])]
    terms = [GlossaryEntry.model_validate(e) for e in data.get("terms", [])]
    return conventions, terms


def write_glossary(
    path: Path, conventions: list[str], terms: list[GlossaryEntry]
) -> None:
    """Persist the shared glossary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"conventions": conventions, "terms": [e.model_dump() for e in terms]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def seed_glossary(path: Path, conventions: list[str]) -> None:
    """Seed the glossary with the plan's conventions, preserving coined terms."""
    _, terms = load_glossary(path)
    write_glossary(path, conventions, terms)


def define_term_in_glossary(path: Path, term: str, meaning: str) -> DefineTermResult:
    """Register a term unless a sibling already defined it — first definition wins."""
    conventions, terms = load_glossary(path)
    for entry in terms:
        if entry.term.casefold() == term.casefold():
            return DefineTermResult(
                term=entry.term, meaning=entry.meaning, already_defined=True
            )
    terms.append(GlossaryEntry(term=term, meaning=meaning))
    write_glossary(path, conventions, terms)
    return DefineTermResult(term=term, meaning=meaning, already_defined=False)


def make_glossary_tools(glossary_path: Path) -> list[LupMcpTool]:
    """MCP tools for the shared cross-writer glossary.

    Parallel section writers share only the filesystem, so both tools read the
    glossary file fresh on every call. A term, once defined, is never
    overwritten — the first definition wins and later writers are handed it,
    which keeps independently-written sections from coining rival names for the
    same thing.
    """

    async def handle_define(inp: DefineTermInput) -> DefineTermResult:
        return define_term_in_glossary(glossary_path, inp.term, inp.meaning)

    async def handle_lookup(inp: LookupTermsInput) -> GlossaryView:
        conventions, terms = load_glossary(glossary_path)
        if inp.contains:
            needle = inp.contains.casefold()
            terms = [e for e in terms if needle in e.term.casefold()]
        return GlossaryView(conventions=conventions, terms=terms)

    return [
        build_stage_tool(
            "define_term",
            (
                "Register a term, name, symbol, or abbreviation in the shared "
                "glossary so the section writers working in parallel use it the "
                "same way. Call this the moment you coin anything the plan's "
                "conventions don't already cover. If a sibling already defined "
                "the term, the tool returns their canonical meaning instead of "
                "overwriting it — adopt it so the assembled piece stays "
                "consistent."
            ),
            DefineTermInput,
            handle_define,
        ),
        build_stage_tool(
            "lookup_terms",
            (
                "Read the shared glossary: the plan's conventions plus every "
                "term sibling writers have coined so far. Call this before "
                "naming a key concept or introducing notation, so you reuse an "
                "existing term instead of inventing a rival one for the same "
                "thing."
            ),
            LookupTermsInput,
            handle_lookup,
        ),
    ]
