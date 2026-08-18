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
from typing import Awaitable, Callable, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lup.mcp import LupMcpTool, ToolError, ToolResponse, mcp_response
from lup.telemetry.metrics import collector as metrics_collector
from lup.types import JsonObject

from inkwell.agent.book import BookLayout, ProposedChapter, ProposedReference
from inkwell.agent.format_checks import DeclaredCheck
from inkwell.agent.glossary import (
    DefineTermResult,
    GlossaryScope,
    GlossaryView,
    define_term_in_glossary,
    read_glossary,
)
from inkwell.agent.models import (
    Assumption,
    AssumptionsList,
    AuthorNote,
    FindingDisposition,
    ResearchCompilation,
    ResearchFinding,
    ResearchQuestion,
    ResearchSource,
    ReviewFinding,
    ReviewOutput,
    RewriteDispositions,
    SectionPlan,
    SourceQuote,
)
from inkwell.agent.provenance import (
    DEFAULT_VENUE_RULES,
    UNDATED,
    Acquisition,
    EvidentialRole,
    SourceProvenance,
    Venue,
    VenueRules,
    calendar_date,
)

logger = logging.getLogger(__name__)


def build_stage_tool(
    tool_name: str,
    description: str,
    input_model: type[BaseModel],
    handler: Callable[..., Awaitable[BaseModel]],
) -> LupMcpTool:
    """Construct an MCP tool from a handler closure."""

    async def wrapper(args: JsonObject) -> ToolResponse:
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

    return LupMcpTool(
        name=tool_name,
        description=description,
        input_schema=input_model.model_json_schema(),
        handler=wrapper,
        call_handler=handler,
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
        description="General direction and preferences from the author, in prose"
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "The author's must/must-not statements distilled from the brief "
            "into short, checkable items (e.g. 'self-contained proofs — do "
            "not cite the source for deliverable content', 'define or rename "
            "borrowed abbreviations', 'least significant digit first'). One "
            "imperative per item, in the author's own terms. A checklist for "
            "reviewers; the author's direction itself stays the authority."
        ),
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


class PlanFile(BaseModel):
    """The plan artifact as it stands mid-stage — a prefix of an `ArticlePlan`.

    `PlanCollector` rewrites it after every incremental tool call, so a reader
    can arrive before `set_plan_header` has fired or between two `add_section`
    calls. The header fields stay `None` until that tool sets them and are
    written out only once they are, so a completed plan whose header never
    arrived still fails `ArticlePlan` validation loudly rather than reaching
    the next stage with an empty title.
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    thesis: str | None = None
    target_format: str | None = None
    author_direction: str | None = None
    voice_notes: str | None = None
    constraints: list[str] | None = None
    deliverables: list[str] | None = None
    conventions: list[str] | None = None
    sections: list[SectionPlan] = Field(default_factory=list)
    research_questions: list[ResearchQuestion] = Field(default_factory=list)
    source_quotes: list[SourceQuote] = Field(default_factory=list)


class PlanCollector:
    """Accumulates incremental plan tool calls. Persists to JSON after each call."""

    def __init__(
        self,
        output_path: Path,
        on_save: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.output_path = output_path
        self.on_save = on_save
        self.plan = PlanFile()

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.plan.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
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
        collector.plan = collector.plan.model_copy(update=inp.model_dump())
        await collector.save_and_notify()
        return ToolOk()

    async def handle_section(inp: AddSectionInput) -> ToolOk:
        collector.plan.sections.append(SectionPlan.model_validate(inp.model_dump()))
        await collector.save_and_notify()
        return ToolOk()

    async def handle_question(inp: AddResearchQuestionInput) -> ToolOk:
        collector.plan.research_questions.append(
            ResearchQuestion.model_validate(inp.model_dump())
        )
        await collector.save_and_notify()
        return ToolOk()

    async def handle_quote(inp: AddSourceQuoteInput) -> ToolOk:
        collector.plan.source_quotes.append(
            SourceQuote.model_validate(inp.model_dump())
        )
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
# Book layout collector + tools
# ---------------------------------------------------------------------------


class SetBookTitleInput(BaseModel):
    title: str = Field(description="What the book is called")


class AddChapterInput(BaseModel):
    chapter: ProposedChapter = Field(
        description=(
            "The chapter to append to the reading order. Its `key` is a stable "
            "slug naming the chapter itself, not its position — spell it the "
            "same way every time this book is laid out, so the chapter keeps "
            "the ordinal it was already given. Ordinals are assigned for you."
        )
    )


class AddCrossReferenceInput(BaseModel):
    reference: ProposedReference = Field(
        description=(
            "What one chapter needs from another, by the two chapters' keys. "
            "Add both chapters before the reference that links them."
        )
    )


class BookCollector:
    """Accumulates the book layout, persisting after each call.

    The same shape as :class:`PlanCollector` and for the same reason: a stage
    that died halfway leaves a readable prefix rather than nothing. Ordinals are
    absent throughout — this holds what the stage proposed, and
    :meth:`~inkwell.agent.book.BookOutline.relaid` is what turns keys into
    numbers against what the book has already handed out.
    """

    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.layout = BookLayout()

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.layout.model_dump_json(indent=2), encoding="utf-8"
        )

    def keyed(self, key: str) -> bool:
        """Whether a chapter under this key has been added yet."""
        return any(held.key == key for held in self.layout.chapters)


def make_book_tools(collector: BookCollector) -> list[LupMcpTool]:
    """Create MCP tools for laying a book's chapters and links out.

    Each tool refuses what the outline could not later resolve — a key used
    twice, a reference to a chapter nobody declared — so the stage is told
    where it went wrong while it can still fix it, rather than having the link
    dropped silently when the layout is numbered.
    """

    async def handle_title(inp: SetBookTitleInput) -> ToolOk:
        collector.layout.title = inp.title
        collector.save()
        return ToolOk()

    async def handle_chapter(inp: AddChapterInput) -> ToolOk:
        if collector.keyed(inp.chapter.key):
            raise ToolError(
                f"This book already has a chapter keyed {inp.chapter.key!r}. A "
                "key names one chapter, so give this one a key of its own."
            )
        collector.layout.chapters.append(inp.chapter)
        collector.save()
        return ToolOk()

    async def handle_reference(inp: AddCrossReferenceInput) -> ToolOk:
        missing = [
            key
            for key in (inp.reference.from_key, inp.reference.to_key)
            if not collector.keyed(key)
        ]
        if missing:
            raise ToolError(
                f"No chapter of this book is keyed {', '.join(missing)}. Add "
                "the chapters with add_chapter first, then link them."
            )
        collector.layout.references.append(inp.reference)
        collector.save()
        return ToolOk()

    return [
        build_stage_tool(
            "set_book_title",
            "Set what the book is called. Call this once, before adding chapters.",
            SetBookTitleInput,
            handle_title,
        ),
        build_stage_tool(
            "add_chapter",
            (
                "Append a chapter to the book's reading order. Call once per "
                "chapter, in the order a reader meets them. You supply the "
                "key, title, and one-line thesis; the ordinal is assigned for "
                "you and held fixed across layouts."
            ),
            AddChapterInput,
            handle_chapter,
        ),
        build_stage_tool(
            "add_cross_reference",
            (
                "Record what one chapter needs from another — the term, "
                "result, or claim it carries across, and which way the "
                "dependency runs. This is what a chapter written months later, "
                "on its own, reads to know what it may lean on and what it "
                "owes the chapters after it."
            ),
            AddCrossReferenceInput,
            handle_reference,
        ),
    ]


# ---------------------------------------------------------------------------
# Format checks a custom format declares for itself
# ---------------------------------------------------------------------------


class DeclaredChecks(BaseModel):
    """The rows a run declared for a format Python does not describe."""

    checks: list[DeclaredCheck] = Field(
        default_factory=list, description="The declared rows, in declared order"
    )


class DeclareFormatCheckInput(BaseModel):
    check: DeclaredCheck = Field(
        description=(
            "The row to declare. `kind` selects which row it is and the rest "
            "of the fields are that row's own; every kind carries a `name` the "
            "report prints and a `rule` — the one sentence the row measures, "
            "which the writer is shown before drafting."
        )
    )


class FormatCheckCollector:
    """Accumulates the rows a custom format declares, persisting after each.

    A format invented for one run gets the same checking a built-in one has,
    because the rows it declares here are the same declarations Python uses.
    """

    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.declared = DeclaredChecks()

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.declared.model_dump_json(indent=2), encoding="utf-8"
        )


def load_declared_checks(path: Path) -> list[DeclaredCheck]:
    """The rows a run declared for its format, or none when it declared any.

    Validated back through the same union the tool validated them into, so a
    row that survives the round trip is a row the checker can run.
    """
    if not path.exists():
        return []
    return DeclaredChecks.model_validate_json(path.read_text(encoding="utf-8")).checks


def make_format_check_tools(collector: FormatCheckCollector) -> list[LupMcpTool]:
    """The tool a custom-format run declares its own checkable rules with."""

    async def handle_check(inp: DeclareFormatCheckInput) -> ToolOk:
        collector.declared.checks.append(inp.check)
        collector.save()
        return ToolOk()

    return [
        build_stage_tool(
            "declare_format_check",
            (
                "Declare one checkable rule for this run's output format. The "
                "built-in formats declare their rules in Python; a format "
                "invented for this run declares them here, so its rules are "
                "measured rather than merely stated.\n\n"
                "Reach for this when the format description carries a rule a "
                "machine could verify off the finished draft — a banned "
                "phrase, a length, a bolding convention, a rhythm — or a rule "
                "only a reader could judge, which the `judged` kind spends a "
                "reviewer on. Each row is re-read against every draft and "
                "reported back to the rewrite stage. Rows are advisory: they "
                "never block a stage, and the author's voice outranks them.\n\n"
                "Declare a row once per rule. A format whose description "
                "carries nothing measurable needs no rows."
            ),
            DeclareFormatCheckInput,
            handle_check,
        ),
    ]


# ---------------------------------------------------------------------------
# Research collector + tools
# ---------------------------------------------------------------------------


class ResearchSourceInput(BaseModel):
    """One source cited for one finding, recorded as what was done to get it.

    Nothing here asks the agent to rate the source. The venue is computed from
    the acquisition, and the two fields that are judgements — the evidential role
    and an override of the derived venue — are the ones the validators below
    refuse to accept unsupported.
    """

    title: str = Field(description="Source title")
    acquisition: Acquisition = Field(
        description=(
            "How you got this document: copy the `acquisition` object out of "
            "the tool result that produced it — `path` (which research tool), "
            "`url` (or the file path, for the author's own source material), "
            "and `published` where that tool reported a date. The venue is "
            "derived from it: path='arxiv' is a preprint, "
            "path='source_document' is the author's own material, and for "
            "path='url_fetch' or path='exa_search' the host decides. This "
            "records what you did, not what you think of the source."
        )
    )
    relevance: str = Field(description="Why this source matters")
    key_excerpt: str = Field(description="Most relevant excerpt (exact quote)")
    role: EvidentialRole = Field(
        description=(
            "What this source is for the claim you cite it for. 'primary': the "
            "claim originates here — the paper reporting the result, the "
            "dataset holding the number, an author's own words for their own "
            "thesis. 'commentary': this document discusses or relays a claim "
            "that originated elsewhere, and attributed_to names whose — "
            "Yudkowsky writing about I. J. Good's intelligence-explosion "
            "thesis is commentary on Good, not the primary source for it. "
            "'background': context, definition, or framing, not evidence for a "
            "specific claim."
        )
    )
    attributed_to: str = Field(
        default="",
        description=(
            "Whose claim a commentary source relays — the person or work it "
            "originates with ('I. J. Good', 'Kahneman & Tversky 1979'). "
            "Required for role='commentary': without it nothing downstream can "
            "see that the primary source is still missing."
        ),
    )
    published: str = Field(
        default="",
        description=(
            "The source's publication date, needed only where the acquisition "
            "reported none. ISO 8601 at whatever precision the source states "
            "('2024', '2024-03', '2024-03-14'), or 'undated' when the source "
            "genuinely carries no date — an explicit 'undated' is what lets a "
            "later check tell a dateless source from an unasked question."
        ),
    )
    locator: str = Field(
        default="",
        description=(
            "Where the excerpt lives: page number(s) for documents "
            "('p. 142'), chapter/section, or URL fragment. Required when "
            "the source is the author's source document."
        ),
    )
    organization: str = Field(
        default="",
        description=(
            "Who published this — the journal, lab, agency, outlet, or site "
            "('Nature', 'RAND', 'OpenAI', 'the Guardian'). Read it off the "
            "document; leave it empty rather than inferring one from the URL. "
            "This is what makes a section leaning on one lab's output visible "
            "as such, so an empty field is recorded as unknown, not as spread."
        ),
    )
    authors: list[str] = Field(
        default_factory=list,
        description=(
            "Whose names are on the document, as it gives them ('Kahneman, D.'). "
            "Empty where it carries no byline — an institutional report often "
            "does not. Cite the same three authors through a whole section and "
            "the distribution says so."
        ),
    )
    venue_override: Venue | None = Field(
        default=None,
        description=(
            "Override the venue derived from the acquisition, for what the "
            "derivation cannot see: a journal paper served from an author's "
            "personal site, a lab's report posted to a forum. Costs an "
            "override_reason, which is recorded beside the source."
        ),
    )
    override_reason: str = Field(
        default="",
        description=(
            "Why the derived venue is wrong for this document, in one "
            "sentence. Required with venue_override."
        ),
    )

    @model_validator(mode="after")
    def override_states_a_reason(self) -> Self:
        """An asserted venue costs a reason, so nothing is relabelled silently."""
        if self.venue_override is not None and not self.override_reason.strip():
            raise ValueError(
                "venue_override requires override_reason — say why the venue "
                f"derived from a {self.acquisition.path!r} acquisition is wrong "
                "for this document."
            )
        return self

    @model_validator(mode="after")
    def commentary_names_whose_claim(self) -> Self:
        """Commentary that never says whose claim it relays hides the missing source."""
        if self.role == "commentary" and not self.attributed_to.strip():
            raise ValueError(
                "role='commentary' requires attributed_to — name whose claim "
                "this source relays. If the claim originates in this document, "
                "the role is 'primary'."
            )
        return self

    @model_validator(mode="after")
    def date_is_explicit(self) -> Self:
        """A date, or a stated `undated` — never a blank that reads as neither."""
        recorded = self.acquisition.published or self.published
        if not recorded:
            raise ValueError(
                f"A {self.acquisition.path!r} acquisition reports no publication "
                "date, so record one in published: ISO 8601 ('2024', '2024-03', "
                f"'2024-03-14'), or {UNDATED!r} if the source carries none."
            )
        if recorded != UNDATED and calendar_date(recorded) is None:
            raise ValueError(
                f"{recorded!r} is not a date a check can read. Use ISO 8601 "
                f"('2024', '2024-03', '2024-03-14') or {UNDATED!r}."
            )
        return self

    @model_validator(mode="after")
    def document_is_locatable(self) -> Self:
        """A citation has to say where the excerpt is, in the author's own material."""
        if not self.acquisition.url.strip():
            raise ValueError(
                "acquisition.url is required — the URL the document was fetched "
                "from, or the file path of the author's source material."
            )
        if self.acquisition.path == "source_document" and not self.locator.strip():
            raise ValueError(
                "A source_document acquisition requires a locator — the page or "
                "section the excerpt is on ('p. 142', '§3.2'), so a reader can "
                "check the quote against the document."
            )
        return self

    def recorded(self, rules: VenueRules = DEFAULT_VENUE_RULES) -> ResearchSource:
        """This source as it is stored, with its venue derived from the acquisition."""
        return ResearchSource(
            title=self.title,
            url=self.acquisition.url,
            relevance=self.relevance,
            key_excerpt=self.key_excerpt,
            locator=self.locator,
            provenance=SourceProvenance(
                venue=self.venue_override or self.acquisition.venue(rules),
                role=self.role,
                published=self.acquisition.published or self.published,
                acquired_via=self.acquisition.path,
                domain=self.acquisition.host(),
                organization=self.organization,
                authors=self.authors,
                attributed_to=self.attributed_to,
                venue_reason=self.override_reason,
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

    def recorded(self, rules: VenueRules = DEFAULT_VENUE_RULES) -> ResearchFinding:
        """The finding as it is stored, each source's venue derived as it is built."""
        return ResearchFinding(
            question=self.question,
            answer=self.answer,
            origin=self.origin,
            confidence=self.confidence,
            sources=[source.recorded(rules) for source in self.sources],
            data_points=self.data_points,
        )


class FindingRecorded(ToolOk):
    """What recording a finding leaves the researcher to act on."""

    provenance_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "What the provenance you recorded shows is missing — a claim "
            "reached only through commentary on it, an answer resting on "
            "background alone. The artifact keeps these, and the fact-check "
            "reviewer reads them; close them now rather than leaving them."
        ),
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
        self.research = ResearchCompilation(findings=[])

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.research.model_dump_json(indent=2), encoding="utf-8"
        )

    async def save_and_notify(self) -> None:
        self.save()
        if self.on_save is not None:
            try:
                await self.on_save()
            except (OSError, RuntimeError):
                logger.warning("ResearchCollector on_save failed", exc_info=True)


def make_research_output_tools(
    collector: ResearchCollector,
    *,
    venue_rules: VenueRules = DEFAULT_VENUE_RULES,
) -> list[LupMcpTool]:
    """MCP tools for the researcher to record findings incrementally.

    ``venue_rules`` is what venue each acquisition implies; a caller whose
    sources are not the open web passes its own table.
    """

    async def handle_finding(inp: RecordFindingInput) -> FindingRecorded:
        finding = inp.recorded(venue_rules)
        claims_the_document = inp.origin in ("source_document", "mixed")
        if claims_the_document and not finding.cites_source_document():
            raise ToolError(
                f"origin={inp.origin!r} says this was answered from the author's "
                "own source material, but no source records a 'source_document' "
                "acquisition. Read the document (consult_source, Read) and "
                "record the excerpt with its page or section, or record "
                "this finding as origin='external' — an external work's wording is "
                "not evidence of what the source document says."
            )
        collector.research.findings.append(finding)
        await collector.save_and_notify()
        return FindingRecorded(provenance_gaps=finding.provenance_gaps())

    async def handle_suggestion(inp: SuggestAdditionInput) -> ToolOk:
        collector.research.suggested_additions.append(inp.suggestion)
        await collector.save_and_notify()
        return ToolOk()

    async def handle_context(inp: SetAdditionalContextInput) -> ToolOk:
        collector.research.additional_context = inp.context
        await collector.save_and_notify()
        return ToolOk()

    return [
        build_stage_tool(
            "record_finding",
            (
                "Record a research finding for one question. Call once per "
                "research question after investigating it. Include all "
                "sources with their acquisition records and key excerpts, your "
                "synthesized answer, and confidence level. Each source carries "
                "two independent things: its venue, which is derived from the "
                "acquisition you paste in rather than asserted, and its role "
                "for this claim — primary where the claim originates in that "
                "document, commentary where it relays someone else's, naming "
                "whose. The tool answers with any gap the record shows, such as "
                "a thesis reached only through commentary on it. Set origin "
                "honestly: claims about the "
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
        self.review = ReviewOutput(findings=[])

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.review.model_dump_json(indent=2), encoding="utf-8"
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
        collector.review.findings.append(
            ReviewFinding(reviewer=collector.reviewer, **inp.model_dump())
        )
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


class DispositionCollector:
    """Accumulates what the rewrite decided about each review finding."""

    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.record = RewriteDispositions()

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.record.model_dump_json(indent=2), encoding="utf-8"
        )


class RecordDispositionInput(BaseModel):
    tag: str = Field(
        description="The finding's tag, exactly as the annotated draft marks it"
    )
    action: Literal["applied", "folded", "rejected"] = Field(
        description=(
            "'applied': the draft now does what the finding asked. 'folded': "
            "handled as part of a larger change rather than on its own terms. "
            "'rejected': deliberately not done."
        )
    )
    reason: str = Field(
        description=(
            "One line. For 'applied', what the draft now says; for 'rejected', "
            "why the finding does not hold."
        )
    )


def make_disposition_tools(collector: DispositionCollector) -> list[LupMcpTool]:
    """The tool the rewrite answers each review finding through."""

    async def handle_disposition(inp: RecordDispositionInput) -> ToolOk:
        collector.record.dispositions.append(FindingDisposition(**inp.model_dump()))
        collector.save()
        return ToolOk()

    return [
        build_stage_tool(
            "record_disposition",
            (
                "Record what you did about one review finding, by its tag. Call "
                "this for every finding in the annotated draft — including the "
                "ones you decide against, which is the whole point: a finding "
                "you rejected on the merits and one you never read look the "
                "same in the finished piece. Reviewers cost real money per run, "
                "and this record is what says whether that bought anything."
            ),
            RecordDispositionInput,
            handle_disposition,
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
        self.assumptions = AssumptionsList(items=[])

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            self.assumptions.model_dump_json(indent=2), encoding="utf-8"
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
        collector.assumptions.items.append(Assumption.model_validate(inp.model_dump()))
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
        logger.info("[%s] Author note: %s", stage, inp.note)
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
# Shared glossary — the tools a writer reaches the ledger in agent/glossary.py
# through; a caller that only reads what the piece has named goes there direct
# ---------------------------------------------------------------------------


class DefineTermInput(BaseModel):
    term: str = Field(
        description="The term, name, symbol, or abbreviation you are introducing"
    )
    meaning: str = Field(description="Its definition — how every section should use it")
    aliases: list[str] = Field(
        default=[],
        description=(
            "The other names for this same thing that you weighed and are not "
            "using — the rival phrasings, spellings, and abbreviations. Naming "
            "them here is what lets a later chapter reaching for one be handed "
            "this term instead of quietly renaming it"
        ),
    )


class LookupTermsInput(BaseModel):
    contains: str = Field(
        default="",
        description=(
            "Optional substring filter over terms; empty returns the whole "
            "shared glossary."
        ),
    )


def make_glossary_tools(scope: GlossaryScope) -> list[LupMcpTool]:
    """MCP tools for the shared glossary.

    Parallel section writers share only the filesystem, so both tools read the
    glossary fresh on every call, and neither handler awaits between reading
    and writing — siblings run on one event loop, so a read-modify-write that
    never yields is the whole of the guarantee among them. Chapter runs share
    no loop, and there the guarantee is the partition: each writes only its own
    chapter's file, atomically.

    A term, once defined, is never overwritten — the first definition wins and
    later writers are handed it, whether the first was a sibling section this
    afternoon or a chapter of the same book written last month. That holds for
    the names an entry records as rejected too, so a writer who says which
    rival phrasings they are not using binds the later chapters that would
    otherwise reach for one.
    """

    async def handle_define(inp: DefineTermInput) -> DefineTermResult:
        return define_term_in_glossary(scope, inp.term, inp.meaning, inp.aliases)

    async def handle_lookup(inp: LookupTermsInput) -> GlossaryView:
        glossary = read_glossary(scope)
        if not inp.contains:
            return glossary
        needle = inp.contains.casefold()
        return GlossaryView(
            conventions=glossary.conventions,
            terms=[e for e in glossary.terms if needle in e.term.casefold()],
        )

    return [
        build_stage_tool(
            "define_term",
            (
                "Register a term, name, symbol, or abbreviation in the shared "
                "glossary so the section writers working in parallel use it the "
                "same way. Call this the moment you coin anything the plan's "
                "conventions don't already cover, and list under 'aliases' the "
                "rival names you considered and rejected. If the term is "
                "already defined — by a sibling section, or by another chapter "
                "of this book, under this name or under one it recorded as "
                "rejected — the tool returns that canonical meaning instead of "
                "overwriting it — adopt it so the assembled piece stays "
                "consistent."
            ),
            DefineTermInput,
            handle_define,
        ),
        build_stage_tool(
            "lookup_terms",
            (
                "Read the shared glossary: this chapter's conventions plus "
                "every term coined so far — by sibling writers, and by the "
                "other chapters of this book. Call this before naming a key "
                "concept or introducing notation, so you reuse an existing term "
                "instead of inventing a rival one for the same thing."
            ),
            LookupTermsInput,
            handle_lookup,
        ),
    ]
