"""Output models for the inkwell writing agent.

Defines the structured data flowing through the writing pipeline:
conversation extraction -> planning -> research -> writing -> review -> rewrite.

Also defines models for the interaction-robust pipeline: comment classification,
restart strategies, assumptions, and pipeline snapshot state.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict, Union

from pydantic import BaseModel, Field, field_validator

from lup.client import CostState
from lup.history import SessionResult

from inkwell.agent.tools.stage_outputs import AuthorNote


class SourceQuote(BaseModel):
    """An exact quote from the source material worth preserving verbatim."""

    text: str = Field(description="The exact quote text")
    speaker: str = Field(description="Who said it (user name, 'claude', etc.)")
    context: str = Field(description="Why this quote matters for the article")


class ResearchQuestion(BaseModel):
    """A question the researcher should investigate."""

    question: str = Field(description="What to research")
    section: str = Field(description="Which section this research supports")
    priority: int = Field(
        default=1, ge=1, le=3, description="1=critical, 2=important, 3=nice-to-have"
    )


class SectionPlan(BaseModel):
    """Plan for one section of the article."""

    title: str = Field(description="Section heading")
    summary: str = Field(description="What this section should cover (2-3 sentences)")
    key_points: list[str] = Field(description="Specific points to make in this section")
    quotes_to_include: list[str] = Field(
        default_factory=list,
        description="Indices/references to SourceQuotes to weave in",
    )


class ArticlePlan(BaseModel):
    """The structured plan extracted from source material."""

    title: str = Field(description="Working title for the article")
    thesis: str = Field(description="Core argument or insight in one sentence")
    target_format: str = Field(
        description="Output format: 'academic', 'lesswrong', 'twitter', 'blog', 'dialog', 'memo', or 'custom:<description>'. Choose based on what best fits the content."
    )
    sections: list[SectionPlan] = Field(description="Ordered list of planned sections")
    research_questions: list[ResearchQuestion] = Field(
        description="Questions the researcher should investigate"
    )
    source_quotes: list[SourceQuote] = Field(
        description="Exact quotes from source material to preserve"
    )
    author_direction: str = Field(
        description="General direction, constraints, or preferences from the author"
    )
    deliverables: list[str] = Field(
        default_factory=list,
        description=(
            "The author's contract: concrete outputs in their own terms. "
            "Immutable through refinement — every stage delivers these, "
            "and reviewers flag violations as critical."
        ),
    )
    conventions: list[str] = Field(
        default_factory=list,
        description=(
            "Shared conventions every section must follow: recurring terms "
            "and what they mean, names for key concepts, notational or "
            "formatting choices. The channel that keeps independently "
            "written sections consistent."
        ),
    )
    voice_notes: str = Field(
        description="Observations about the author's tone, style, and voice from the source conversation"
    )


class ResearchSource(BaseModel):
    """A source found during research."""

    title: str = Field(description="Source title")
    url: str = Field(description="Source URL, or file path for source documents")
    relevance: str = Field(description="Why this source matters")
    key_excerpt: str = Field(
        description="Most relevant excerpt (exact quote with attribution)"
    )
    locator: str = Field(
        default="",
        description=(
            "Where the excerpt lives: page number(s) for documents "
            "('p. 142'), chapter/section, or URL fragment"
        ),
    )


class ResearchFinding(BaseModel):
    """A compiled research finding for a specific question."""

    question: str = Field(description="The original research question")
    answer: str = Field(description="Synthesized answer based on sources")
    origin: Literal["source_document", "external", "mixed", "author_unverified"] = (
        Field(
            default="external",
            description=(
                "Provenance of the answer. 'source_document': the author's own "
                "source material — every such claim must carry a verbatim quote "
                "with a locator in sources. 'external': other works (papers, "
                "web). 'mixed': both. A claim about what the source document "
                "says, backed only by external works, is 'external' — never "
                "blend an external convention into a source_document claim. "
                "'author_unverified': a specific the author asserts (a named "
                "event, number, ratio, study, anecdote) that research could "
                "neither confirm nor refute. It is load-bearing voice, not a "
                "gap to fill: preserve the author's claim verbatim in the answer "
                "and flag it for the author — never drop it or swap in a generic "
                "verified proxy."
            ),
        )
    )
    sources: list[ResearchSource] = Field(description="Sources consulted")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the finding (0-1)"
    )
    data_points: list[str] = Field(
        default_factory=list,
        description="Specific numbers, dates, or facts found",
    )


class ResearchCompilation(BaseModel):
    """Complete research output from the researcher agent."""

    findings: list[ResearchFinding] = Field(description="Research findings")
    additional_context: str = Field(
        default="",
        description="Background context that doesn't fit a specific question",
    )
    suggested_additions: list[str] = Field(
        default_factory=list,
        description="Suggestions for the article that emerged from research",
    )


class SectionDraft(BaseModel):
    """A drafted section from a section writer agent."""

    title: str = Field(description="Section heading")
    content: str = Field(description="Full markdown content of the section")
    word_count: int = Field(default=0, description="Word count of the section")
    sources_used: list[str] = Field(
        default_factory=list, description="URLs of sources referenced"
    )
    questions_for_author: list[AuthorNote] = Field(
        default_factory=list,
        description="Questions the writer wants to ask the author about this section",
    )

    @field_validator("questions_for_author", mode="before")
    @classmethod
    def coerce_strings_to_author_notes(
        cls, v: list[str | AuthorNote | dict[str, str]]
    ) -> list[AuthorNote | dict[str, str]]:
        """Accept plain strings from old snapshots."""
        return [AuthorNote(note=item) if isinstance(item, str) else item for item in v]


class ReviewFinding(BaseModel):
    """A finding from one of the reviewer agents."""

    reviewer: str = Field(
        description="Which reviewer: 'narrative', 'factcheck', 'style', 'source_fidelity', or 'coverage'"
    )
    severity: Literal["critical", "suggestion", "praise"] = Field(
        description="'critical', 'suggestion', or 'praise'"
    )
    location: str = Field(description="Section or paragraph reference")
    issue: str = Field(description="What the reviewer found")
    suggestion: str = Field(default="", description="Suggested fix or improvement")
    text_excerpt: str = Field(
        default="",
        description="Exact verbatim text excerpt from the draft that this finding refers to, for anchoring comments",
    )


class ReviewOutput(BaseModel):
    """Structured output from a reviewer agent."""

    findings: list[ReviewFinding] = Field(description="All findings from this reviewer")


class MergedDraft(BaseModel):
    """Output of the coherence editor — a unified draft from independent sections."""

    content: str = Field(description="The complete merged draft in markdown")
    changes_made: list[str] = Field(
        default_factory=list,
        description="Summary of edits made during merging (transitions, deduplication, etc.)",
    )


# ---------------------------------------------------------------------------
# Comment classification (used by Comment Watcher)
# ---------------------------------------------------------------------------


class ClassifiedComment(BaseModel):
    """A GDoc comment classified by impact on the pipeline."""

    comment_id: str = Field(description="Google Drive comment ID")
    content: str = Field(description="Comment text")
    anchor_text: str = Field(
        default="", description="Quoted text the comment is anchored to"
    )
    reply: str = Field(
        default="", description="Author's reply text, if replying to an agent comment"
    )
    impact: Literal[
        "plan_breaking", "stage_local", "clarification", "dismiss", "revert_suggested"
    ] = Field(
        description=(
            "plan_breaking: invalidates thesis/structure, requires re-planning. "
            "stage_local: affects current/next stage only. "
            "clarification: factual correction or scope note. "
            "dismiss: noise — accidental keystroke, formatting artifact, no semantic change. "
            "revert_suggested: accidental damage — text was deleted or mangled unintentionally."
        )
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Semantic tags: 'tone', 'structure', 'fact', 'scope', etc.",
    )
    timestamp: str = Field(default="", description="ISO timestamp when classified")


# ---------------------------------------------------------------------------
# Assumptions stage output
# ---------------------------------------------------------------------------


class Assumption(BaseModel):
    """A single uncertainty surfaced by the assumptions stage."""

    tag: Literal["direction_check", "assumption", "question", "confusion"] = Field(
        description=(
            "direction_check: which way should this go? "
            "assumption: something the agent is assuming without confirmation. "
            "question: a gap in the source material. "
            "confusion: contradictory or unclear information."
        )
    )
    content: str = Field(description="The uncertainty, question, or assumption")
    best_guess: str = Field(
        description="What the agent will proceed with if the author doesn't respond"
    )
    anchor_section: str = Field(description="Which plan section this relates to")


class AssumptionsList(BaseModel):
    """Output of the assumptions stage — all uncertainties surfaced at once."""

    items: list[Assumption] = Field(
        description="All assumptions and questions to surface"
    )


# ---------------------------------------------------------------------------
# Orchestrator: restart strategy
# ---------------------------------------------------------------------------


class PatchAction(BaseModel):
    """Rewrite a specific passage within a section."""

    kind: Literal["patch"] = "patch"
    section: str = Field(description="Section title to patch")
    target_text: str = Field(description="The paragraph or passage to replace")
    instruction: str = Field(description="What to change and why")


class RewriteAction(BaseModel):
    """Full re-research and re-write of a section."""

    kind: Literal["rewrite"] = "rewrite"
    section: str = Field(description="Section title to rewrite from scratch")
    reason: str = Field(description="Why this section needs a full rewrite")
    new_research_questions: list[str] = Field(
        default_factory=list,
        description="Additional research questions for the rewritten section",
    )


class AddAction(BaseModel):
    """Add a new section to the article."""

    kind: Literal["add"] = "add"
    section_plan: SectionPlan = Field(description="Plan for the new section")
    insert_after: str = Field(description="Title of the section to insert after")


class DropAction(BaseModel):
    """Remove a section from the article."""

    kind: Literal["drop"] = "drop"
    section: str = Field(description="Section title to remove")
    reason: str = Field(description="Why this section should be dropped")


class PreserveAction(BaseModel):
    """Keep a section as-is — no changes needed."""

    kind: Literal["preserve"] = "preserve"
    section: str = Field(description="Section title to keep unchanged")


RestartAction = Annotated[
    Union[PatchAction, RewriteAction, AddAction, DropAction, PreserveAction],
    Field(discriminator="kind"),
]


class RestartStrategy(BaseModel):
    """The orchestrator's plan for rework after plan-breaking feedback."""

    new_plan: ArticlePlan | None = Field(
        default=None,
        description="Revised article plan, or None to keep current plan",
    )
    actions: list[RestartAction] = Field(
        description="Action for each section: preserve, patch, rewrite, add, or drop"
    )
    needs_remerge: bool = Field(
        description="Whether the merge step should re-run after executing actions"
    )
    rationale: str = Field(description="Why this strategy was chosen")


# ---------------------------------------------------------------------------
# Pipeline snapshot (accumulated artifacts for state machine)
# ---------------------------------------------------------------------------


class PipelineSnapshot(BaseModel):
    """Accumulated artifacts from the pipeline, used for restarts and state tracking."""

    generation: int = Field(default=0, description="Incremented on each restart")
    stage: str = Field(default="init", description="Current pipeline stage")
    profile: str | None = Field(
        default=None, description="Config profile active when the session started"
    )
    conversation: str = Field(default="", description="Extracted source text")
    raw_sources: list[str] = Field(
        default_factory=list, description="Original source inputs before preprocessing"
    )
    author_instructions: str = Field(
        default="", description="Instructions extracted from freeform source text"
    )
    author_deliverables: list[str] = Field(
        default_factory=list,
        description="Deliverables extracted from the author's instructions",
    )
    runtime_style_refs: list[str] = Field(
        default_factory=list, description="Style reference inputs supplied at runtime"
    )
    source_file_paths: list[str] = Field(
        default_factory=list, description="Extracted source file paths"
    )
    voice_profile: str | None = Field(default=None)
    voice_fingerprint: str = Field(
        default="", description="Hash of voice inputs, used to skip re-analysis"
    )
    voice_file_paths: list[str] = Field(
        default_factory=list, description="Voice analysis artifact paths"
    )
    plan: ArticlePlan | None = Field(default=None)
    research: ResearchCompilation | None = Field(default=None)
    section_drafts: dict[str, SectionDraft] = Field(
        default_factory=dict, description="Section title -> draft"
    )
    merged: MergedDraft | None = Field(default=None)
    findings: list[ReviewFinding] = Field(default_factory=list)
    output: WritingOutput | None = Field(default=None)

    doc_id: str = Field(default="", description="Google Doc ID (for resume)")
    doc_url: str = Field(default="", description="Google Doc URL (for resume)")
    seen_comment_ids: set[str] = Field(
        default_factory=set, description="Comment IDs already processed"
    )
    agent_comment_ids: set[str] = Field(
        default_factory=set, description="Comment IDs posted by the agent"
    )
    seen_source_comment_ids: set[str] = Field(
        default_factory=set, description="Source-doc comment IDs already processed"
    )
    source_doc_id: str = Field(
        default="", description="Google Doc ID of the source document, if any"
    )
    pending_questions: list[str] = Field(
        default_factory=list, description="Unanswered questions for the author"
    )
    cost_state: CostState | None = Field(
        default=None,
        description="Accumulated cost/token state, carried across process restarts",
    )
    session_ids: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Stage/section label -> SDK session id, so a resumed run can "
            "continue an interrupted agent's own conversation rather than "
            "restart it from a blank context"
        ),
    )


# ---------------------------------------------------------------------------
# Final output
# ---------------------------------------------------------------------------


class StageCostBreakdown(TypedDict):
    """Per-stage cost data populated after pipeline completion."""

    cost_usd: float
    duration_s: float
    input_tokens: int
    output_tokens: int
    calls: int


class WritingOutput(BaseModel):
    """Final structured output from any writing session."""

    title: str = Field(description="Final article title")
    content: str = Field(default="", description="Full article content in markdown")
    google_doc_id: str = Field(default="", description="Google Doc ID")
    google_doc_url: str = Field(
        default="", description="URL of the Google Doc with the article"
    )
    word_count: int = Field(default=0, description="Total word count")
    review_findings: list[ReviewFinding] = Field(
        default_factory=list, description="Findings from all reviewers"
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Questions for the author left as Google Doc comments",
    )
    voice_profile: str | None = Field(
        default=None,
        description="Author voice profile used during writing",
    )
    summary: str = Field(default="", description="Brief 1-2 sentence editorial summary")
    stage_costs: dict[str, StageCostBreakdown] = Field(
        default_factory=dict,
        description="Per-stage cost breakdown populated after pipeline completion",
    )


AgentSessionResult = SessionResult[WritingOutput]
