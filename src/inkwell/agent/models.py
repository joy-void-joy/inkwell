"""Output models for the inkwell writing agent.

Defines the structured data flowing through the writing pipeline:
conversation extraction -> planning -> research -> writing -> review -> rewrite.

Also defines models for the interaction-robust pipeline: comment classification,
restart strategies, assumptions, and pipeline snapshot state.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lup.runtime.usage import CostAccumulator
from lup.types import JsonObject, JsonValue, StringMap
from lup.workspace.history import SessionResult

from inkwell.agent.book import BookOutline, ChapterPlacement
from inkwell.agent.provenance import SourceProvenance, Venue


type SourceRole = Literal["source", "revision_target", "style_reference", "context"]
"""What one input is to the run that was handed it.

The distinction the pipeline cannot recover on its own is 'source' against
'revision_target': one is content to write from, the other is the piece the
run replaces, and finished prose carrying its own citations looks the same
either way. It is declared by the entry point, which already knows.
"""


type ReviewProfile = Literal["full", "manuscript_part"]
"""Which independent review concerns a run buys after drafting."""


type QuestionChannel = Literal["document", "handback"]
"""Where a run's open questions reach somebody who can answer them.

'document' posts them as comments on the run's Google Doc, which is right when
that document is the deliverable and the author is reading it.

'handback' returns them on the output and posts nothing, which is right when
the document is a scratch surface one run of one part created and will not
outlive. A pass over a book of two hundred parts creates two hundred documents;
posting each part's questions into its own is filing them where nobody is
looking. The work has a mailbox that outlives every run, and what comes back
goes there.

Declared by the entry point, which knows what its document is for. The pipeline
cannot recover it: a Doc is a Doc either way.
"""


class AuthorNote(BaseModel):
    """A note from any pipeline stage addressed to the author."""

    note: str = Field(description="The question, flag, or suggestion")
    anchor: str = Field(
        default="",
        description="Section title, quote, or text this note refers to",
    )
    stage: str = Field(default="", description="Pipeline stage that produced this note")


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
    purpose: str = Field(
        default="", description="What this section establishes and why it belongs here"
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Finding references or concrete evidence assigned to this section",
    )
    handoff: str = Field(
        default="", description="What the following section inherits from this one"
    )
    quotes_to_include: list[str] = Field(
        default_factory=list,
        description="Indices/references to SourceQuotes to weave in",
    )


class WordBudget(BaseModel, frozen=True):
    """The word-count interval a finished piece is allowed to occupy."""

    minimum: int = Field(default=0, ge=0, description="Fewest accepted words")
    maximum: int = Field(ge=1, description="Most accepted words")

    @model_validator(mode="after")
    def ordered(self) -> WordBudget:
        """Reject an interval no output could satisfy."""
        if self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        return self

    def accepts(self, words: int) -> bool:
        """Whether a finished piece satisfies this contract."""
        return self.minimum <= words <= self.maximum


class ArticlePlan(BaseModel):
    """The structured plan extracted from source material."""

    title: str = Field(description="Working title for the article")
    thesis: str = Field(description="Core argument or insight in one sentence")
    target_format: str = Field(
        description="Output format: 'academic', 'lesswrong', 'twitter', 'blog', 'dialog', 'memo', or 'custom:<description>'. Choose based on what best fits the content."
    )
    word_budget: WordBudget | None = Field(
        default=None, description="Accepted final word-count interval, when declared"
    )
    placement: ChapterPlacement | None = Field(
        default=None,
        description=(
            "Which chapter of which book this plan is, absent for a standalone "
            "piece. Absent means bookless rather than a book of one chapter: a "
            "piece is only part of a book where the run that produced it was "
            "launched as one, so nothing is silently promoted into a book it "
            "was never placed in"
        ),
    )
    sections: list[SectionPlan] = Field(description="Ordered list of planned sections")
    research_questions: list[ResearchQuestion] = Field(
        description="Questions the researcher should investigate"
    )
    source_quotes: list[SourceQuote] = Field(
        description="Exact quotes from source material to preserve"
    )
    author_direction: str = Field(
        description="General direction and preferences from the author, in prose"
    )
    direction_from: Literal["author", "planner"] = Field(
        default="author",
        description=(
            "Who wrote author_direction and constraints. 'author' where a "
            "person stated them, which makes them fixed. 'planner' where a "
            "reader of the work composed them before research ran: they are "
            "then the best available reading of the evidence rather than "
            "anybody's instruction, and a later stage that finds evidence "
            "against one may overturn it, saying in the plan that it did"
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Soft checklist distilled from author_direction: the author's "
            "must/must-not statements as short, checkable items (e.g. "
            "'self-contained proofs — do not cite the source for deliverable "
            "content', 'define or rename borrowed abbreviations', 'least "
            "significant digit first'). Advisory, not the deliverables "
            "contract — the propagated author direction stays the authority; "
            "this list helps reviewers check the draft systematically."
        ),
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
            "Shared conventions every section of this piece must follow: "
            "recurring terms and what they mean, names for key concepts, "
            "notational or formatting choices. The channel that keeps "
            "independently written sections consistent. These are this plan's "
            "instructions to its own writers — a chapter of a book seeds its "
            "own rather than inheriting a sibling chapter's, while the terms "
            "writers coin are shared across the whole book."
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
    provenance: SourceProvenance | None = Field(
        default=None,
        description=(
            "Venue authority, evidential role, and publication date, derived "
            "from how the document was acquired when the source was recorded. "
            "Absent on an artifact written before provenance was recorded, "
            "which means unrecorded — not undated"
        ),
    )

    def venue(self) -> Venue:
        """The venue research derived, unknown where none was recorded."""
        return self.provenance.venue if self.provenance is not None else "unknown"

    def published(self) -> str:
        """The publication date research recorded, empty where none was."""
        return self.provenance.published if self.provenance is not None else ""


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

    def recorded_provenance(self) -> list[SourceProvenance]:
        """The provenance of every source that carries it."""
        return [s.provenance for s in self.sources if s.provenance is not None]

    def cites_source_document(self) -> bool:
        """Whether any source came from the author's own source material."""
        return any(
            p.acquired_via == "source_document" for p in self.recorded_provenance()
        )

    def provenance_gaps(self) -> list[str]:
        """What the recorded provenance shows this finding is missing.

        Read off the record rather than judged, so a claim reached only through
        someone else's commentary is visible where it was recorded instead of
        waiting for a reviewer to notice. Empty for a finding written before
        provenance was recorded, where there is nothing to read.
        """
        recorded = self.recorded_provenance()
        if not recorded:
            return []

        def gaps() -> Iterator[str]:
            """Every gap the record shows, in terms the researcher can act on."""
            relayed = sorted(
                {
                    p.attributed_to
                    for p in recorded
                    if p.role == "commentary" and p.attributed_to
                }
            )
            if relayed and not any(p.role == "primary" for p in recorded):
                for name in relayed:
                    yield (
                        f"{name}'s claim is recorded only through commentary on "
                        f"it. Read what {name} wrote and record that as the "
                        f"primary source, or say in the answer that this is the "
                        f"commentator's reading of {name}."
                    )
            if all(p.role == "background" for p in recorded):
                yield (
                    "Every source is background — nothing recorded is evidence "
                    "for the answer itself."
                )

        return list(gaps())


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
        cls, v: list[str | AuthorNote | JsonObject]
    ) -> list[AuthorNote | JsonObject]:
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


class FindingDisposition(BaseModel):
    """What the rewrite did about one review finding, and why."""

    tag: str = Field(description="The finding's tag, as the annotated draft marks it")
    action: Literal["applied", "folded", "rejected"] = Field(
        description=(
            "'applied': the draft now does what the finding asked. 'folded': "
            "handled as part of another change rather than on its own terms. "
            "'rejected': deliberately not done"
        )
    )
    reason: str = Field(
        description=(
            "Why, in one line. Required for every action, because 'applied' "
            "with no account of what changed is the same silence as no entry"
        )
    )


class RewriteDispositions(BaseModel):
    """Every finding the rewrite answered for, as it answered.

    The record exists so that whether a reviewer earned its cost is a fact on
    disk rather than an inference from whether a quoted passage survived. A
    rewrite that silently drops half its findings and one that considers and
    rejects them look identical in the finished draft, and until now the
    pipeline could not tell them apart either.
    """

    dispositions: list[FindingDisposition] = Field(
        default_factory=list, description="One entry per finding the rewrite saw"
    )


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


type CommentImpact = Literal[
    "plan_breaking", "stage_local", "clarification", "dismiss", "revert_suggested"
]
"""How an author's GDoc comment bears on the run in flight."""


type AssumptionTag = Literal["direction_check", "assumption", "question", "confusion"]
"""What kind of uncertainty the assumptions stage surfaced."""


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
    impact: CommentImpact = Field(
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


class IntakeRecord(BaseModel):
    """How one comment channel's last poll went, as the run wrote it down.

    A feedback set cannot show the difference between an author who said
    nothing and a Drive that could not be read, and only one of those means a
    comment is sitting unread. The run records which happened beside its
    notes, so a later reader — the author, or whoever is asked why a comment
    went unanswered — can tell them apart without polling Drive a second time
    and getting a different answer.
    """

    channel: str = Field(description="Which document was polled: 'author' or 'source'")
    doc_id: str = Field(description="The document the poll read")
    at: str = Field(description="ISO timestamp of the poll")
    unreachable: str = Field(
        default="", description="Why Drive could not be read, empty when it was"
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Unresolved comment ids the poll read as author feedback",
    )
    offered: list[str] = Field(
        default_factory=list,
        description="Ids this poll handed to its caller as not yet accounted for",
    )


# ---------------------------------------------------------------------------
# Assumptions stage output
# ---------------------------------------------------------------------------


class Assumption(BaseModel):
    """A single uncertainty surfaced by the assumptions stage."""

    tag: AssumptionTag = Field(
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


class RewriteTask(BaseModel):
    """One section queued for a full re-research and re-write."""

    section_plan: SectionPlan
    research_questions: list[str] = Field(default_factory=list)


class RestartQueue(BaseModel):
    """What carrying out a restart strategy leaves for the rewrite stage.

    Two of the five actions cannot be done one at a time — a rewrite and an
    addition are both scheduled work — so they collect here while the ones
    that act immediately act.
    """

    rewrites: list[RewriteTask] = Field(default_factory=list)
    additions: list[SectionPlan] = Field(default_factory=list)

    def pending(self) -> bool:
        """Whether anything was queued for the rewrite stage."""
        return bool(self.rewrites or self.additions)


class RestartTarget(Protocol):
    """What an action needs of the run carrying it out.

    Named as a protocol so the actions stay in this module: a run imports
    them, and an action that imported the run back would close the loop.
    """

    async def patch_section(
        self, section: str, target_text: str, instruction: str
    ) -> None: ...

    def find_section_plan(self, section_title: str) -> "SectionPlan | None": ...

    def drop_section(self, section: str) -> None: ...


class RestartStep(BaseModel):
    """One step of the orchestrator's rework plan.

    The base declares what carrying a step out means and each kind answers or
    declines, so adding a kind is one class rather than an edit to every walk
    that would otherwise have to notice it.
    """

    async def carry_out(self, run: RestartTarget, queue: RestartQueue) -> None:
        """Do nothing, which is what a step that changes nothing means."""


class PatchAction(RestartStep):
    """Rewrite a specific passage within a section."""

    kind: Literal["patch"] = "patch"
    section: str = Field(description="Section title to patch")
    target_text: str = Field(description="The paragraph or passage to replace")
    instruction: str = Field(description="What to change and why")

    async def carry_out(self, run: RestartTarget, queue: RestartQueue) -> None:
        await run.patch_section(self.section, self.target_text, self.instruction)


class RewriteAction(RestartStep):
    """Full re-research and re-write of a section."""

    kind: Literal["rewrite"] = "rewrite"
    section: str = Field(description="Section title to rewrite from scratch")
    reason: str = Field(description="Why this section needs a full rewrite")
    new_research_questions: list[str] = Field(
        default_factory=list,
        description="Additional research questions for the rewritten section",
    )

    async def carry_out(self, run: RestartTarget, queue: RestartQueue) -> None:
        section_plan = run.find_section_plan(self.section)
        if section_plan is not None:
            queue.rewrites.append(
                RewriteTask(
                    section_plan=section_plan,
                    research_questions=self.new_research_questions,
                )
            )


class AddAction(RestartStep):
    """Add a new section to the article."""

    kind: Literal["add"] = "add"
    section_plan: SectionPlan = Field(description="Plan for the new section")
    insert_after: str = Field(description="Title of the section to insert after")

    async def carry_out(self, run: RestartTarget, queue: RestartQueue) -> None:
        queue.additions.append(self.section_plan)


class DropAction(RestartStep):
    """Remove a section from the article."""

    kind: Literal["drop"] = "drop"
    section: str = Field(description="Section title to remove")
    reason: str = Field(description="Why this section should be dropped")

    async def carry_out(self, run: RestartTarget, queue: RestartQueue) -> None:
        run.drop_section(self.section)


class PreserveAction(RestartStep):
    """Keep a section as-is — no changes needed."""

    kind: Literal["preserve"] = "preserve"
    section: str = Field(description="Section title to keep unchanged")


RestartAction = Annotated[
    PatchAction | RewriteAction | AddAction | DropAction | PreserveAction,
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
    review_profile: ReviewProfile = Field(
        default="full", description="Reviewer suite declared for this run"
    )
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
        default_factory=list,
        description="Labels of runtime style references whose voice was analyzed",
    )
    style_ref_samples: list[str] = Field(
        default_factory=list,
        description="Extracted prose of runtime style references, fed to voice analysis",
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
    outline: BookOutline | None = Field(
        default=None,
        description=(
            "The book's order and cross-references as the book stage left "
            "them, absent for a standalone run and for one whose book was laid "
            "out by an earlier run rather than this one"
        ),
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
    seen_comment_ids: list[str] = Field(
        default_factory=list, description="Comment IDs already processed"
    )
    agent_comment_ids: list[str] = Field(
        default_factory=list, description="Comment IDs posted by the agent"
    )
    seen_source_comment_ids: list[str] = Field(
        default_factory=list, description="Source-doc comment IDs already processed"
    )
    source_doc_id: str = Field(
        default="", description="Google Doc ID of the source document, if any"
    )
    pending_questions: list[str] = Field(
        default_factory=list, description="Unanswered questions for the author"
    )
    cost_state: CostAccumulator | None = Field(
        default=None,
        description="Accumulated cost/token state, carried across process restarts",
    )
    session_ids: StringMap = Field(
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


class HistoryOutputData(BaseModel):
    """A saved record's output, read as tolerantly as a reader of one must.

    A record on disk was written by whichever version produced it, so every
    field is optional and anything newer is ignored: a listing of past
    sessions should show what it can rather than refuse the whole record.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    google_doc_url: str = ""
    word_count: int = 0
    paused_after: str = ""
    review_findings: list[JsonValue] = Field(default_factory=list)


class HistorySessionData(BaseModel):
    """One session read back from the record format `lup.workspace.history` saves."""

    model_config = ConfigDict(extra="ignore")

    cost_usd: float | None = None
    duration_seconds: float | None = None
    timestamp: str = ""
    output: HistoryOutputData | None = None
    profile: str | None = None


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
    paused_after: str = Field(
        default="",
        description=(
            "Stage the run paused after when launched with a stop point; empty "
            "for a fully completed run. A paused run carries no finished content "
            "yet — its snapshot lets a resume continue from the next stage."
        ),
    )
    stage_costs: dict[str, StageCostBreakdown] = Field(
        default_factory=dict,
        description="Per-stage cost breakdown populated after pipeline completion",
    )


class AgentSessionResult(SessionResult[WritingOutput]):
    """One writing session's result, and the account that paid for it.

    The profile is inkwell's own: a run is billed to whichever credentials
    its profile names, and a resume has to reach the same one, so the answer
    travels with the result rather than being reconstructed from a snapshot.
    """

    profile: str | None = Field(
        default=None, description="Config profile the session ran under"
    )
