"""Output models for the inkwell writing agent.

Defines the structured data flowing through the writing pipeline:
conversation extraction -> planning -> research -> writing -> review -> rewrite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from lup.history import SessionResult

if TYPE_CHECKING:
    from inkwell.agent.tools.voice import VoiceProfile


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
        description="Output format: 'lesswrong', 'twitter_thread', 'blog', etc."
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
    voice_notes: str = Field(
        description="Observations about the author's tone, style, and voice from the source conversation"
    )


class ResearchSource(BaseModel):
    """A source found during research."""

    title: str = Field(description="Source title")
    url: str = Field(description="Source URL")
    relevance: str = Field(description="Why this source matters")
    key_excerpt: str = Field(
        description="Most relevant excerpt (exact quote with attribution)"
    )


class ResearchFinding(BaseModel):
    """A compiled research finding for a specific question."""

    question: str = Field(description="The original research question")
    answer: str = Field(description="Synthesized answer based on sources")
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
        description="Background context that doesn't fit a specific question"
    )
    suggested_additions: list[str] = Field(
        default_factory=list,
        description="Suggestions for the article that emerged from research",
    )


class SectionDraft(BaseModel):
    """A drafted section from a section writer agent."""

    title: str = Field(description="Section heading")
    content: str = Field(description="Full markdown content of the section")
    word_count: int = Field(description="Word count of the section")
    sources_used: list[str] = Field(
        default_factory=list, description="URLs of sources referenced"
    )
    questions_for_author: list[str] = Field(
        default_factory=list,
        description="Questions the writer wants to ask the author about this section",
    )


class ReviewFinding(BaseModel):
    """A finding from one of the reviewer agents."""

    reviewer: str = Field(
        description="Which reviewer: 'narrative', 'factcheck', or 'style'"
    )
    severity: Literal["critical", "suggestion", "praise"] = Field(
        description="'critical', 'suggestion', or 'praise'"
    )
    location: str = Field(description="Section or paragraph reference")
    issue: str = Field(description="What the reviewer found")
    suggestion: str = Field(description="Suggested fix or improvement")
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
        description="Summary of edits made during merging (transitions, deduplication, etc.)"
    )


class WritingOutput(BaseModel):
    """Final structured output from the writing pipeline."""

    title: str = Field(description="Final article title")
    content: str = Field(default="", description="Full article content in markdown")
    google_doc_id: str = Field(default="", description="Google Doc ID")
    google_doc_url: str = Field(description="URL of the Google Doc with the article")
    word_count: int = Field(description="Total word count")
    sections_completed: int = Field(description="Number of sections written")
    review_findings: list[ReviewFinding] = Field(
        default_factory=list, description="Findings from all reviewers"
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Questions for the author left as Google Doc comments",
    )
    voice_profile: VoiceProfile | None = Field(
        default=None,
        description="Author voice profile used during writing",
    )
    summary: str = Field(description="Brief 1-2 sentence editorial summary")


AgentSessionResult = SessionResult[WritingOutput]
