"""Pydantic models for the web API — request/response schemas and WebSocket messages."""

from typing import Literal

from pydantic import BaseModel, Field


class FormatOption(BaseModel):
    key: str = Field(description="Format identifier (e.g. 'lesswrong', 'custom')")
    label: str = Field(description="Human-readable label")
    accepts_description: bool = Field(
        default=False, description="Whether this format takes a freeform description"
    )


class UploadResult(BaseModel):
    path: str = Field(description="Server-side file path for use as a source")
    filename: str = Field(description="Saved filename")
    size: int = Field(description="File size in bytes")


class CreateSessionRequest(BaseModel):
    sources: list[str] = Field(
        description="Source materials: URLs, Claude share links, file paths, or freeform text"
    )
    refs: list[str] = Field(
        default_factory=list, description="Supplementary reference URLs or file paths"
    )
    target_format: str = Field(default="auto", description="Output format")
    existing_doc_id: str | None = Field(
        default=None, description="Google Doc ID to write into"
    )
    profile: str | None = Field(
        default=None, description="Configuration profile to use for this session"
    )


class SessionAction(BaseModel):
    action: Literal["sync", "done", "quit", "feedback"] = Field(
        description="Action to perform"
    )
    text: str | None = Field(default=None, description="Text for feedback action")


class ResumeSessionRequest(BaseModel):
    from_stage: str | None = Field(
        default=None, description="Resume from after this stage"
    )
    profile: str | None = Field(
        default=None,
        description="Override profile (required if original profile is unknown)",
    )


class StageCostSummary(BaseModel):
    cost_usd: float
    duration_s: float
    input_tokens: int
    output_tokens: int
    calls: int


class CostSnapshot(BaseModel):
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    duration_s: float = 0.0
    stages: dict[str, StageCostSummary] = Field(default_factory=dict)


class SectionInfo(BaseModel):
    title: str
    tab_id: str


class SessionStateSnapshot(BaseModel):
    doc_id: str = ""
    doc_url: str = ""
    title: str = ""
    stage: str = "starting"
    sections: list[SectionInfo] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)


class CompletionOutput(BaseModel):
    title: str = ""
    summary: str = ""
    google_doc_url: str = ""
    word_count: int = 0
    review_findings_count: int = 0


class HistoryOutputData(BaseModel):
    """Nested output from a history session record."""

    model_config = {"extra": "ignore"}

    title: str = ""
    google_doc_url: str = ""
    word_count: int = 0
    review_findings: list[object] = Field(default_factory=list)


class HistorySessionData(BaseModel):
    """Parse a session from lup.history's SessionData format."""

    model_config = {"extra": "ignore"}

    cost_usd: float | None = None
    duration_seconds: float | None = None
    timestamp: str = ""
    output: HistoryOutputData | None = None
    profile: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    title: str = ""
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    stage: str = "starting"
    cost_usd: float = 0.0
    duration_s: float = 0.0
    doc_url: str = ""
    created_at: str = ""
    profile: str | None = None
    checkpoints: list[str] = Field(default_factory=list)


class SessionDetail(BaseModel):
    session_id: str
    title: str = ""
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    state: SessionStateSnapshot
    cost: CostSnapshot
    created_at: str = ""
    profile: str | None = None
    events: list[dict[str, object]] = Field(default_factory=list)
    output: CompletionOutput | None = None
    checkpoints: list[str] = Field(default_factory=list)
