"""Pydantic models for the web API — request/response schemas and WebSocket messages.

The models a *request* validates against are not written here. Each is compiled
from the entry point declaration in :mod:`inkwell.environment.entrypoints`, whose
parameters the typer commands and the browser form are rendered from too, and
named below by subclassing what :func:`request_model` builds — which is what
gives a route's body an annotation without respelling a single field.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from inkwell.agent.config import PipelineStage
from inkwell.environment.entrypoints import (
    RESTART,
    RESUME,
    REVISE,
    RUN,
    WRITE,
    request_model,
)

type SessionStatus = Literal[
    "running", "completed", "failed", "cancelled", "interrupted", "paused"
]
"""Where a session stands: the six states the UI knows how to render."""


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


class UploadTextRequest(BaseModel):
    filename: str = Field(
        description="Desired file name; a text extension (.md/.txt/.html) is enforced"
    )
    content: str = Field(description="Pasted or edited text to save as a file")
    replace_path: str | None = Field(
        default=None,
        description="Existing upload to replace in place (the edit case), so the "
        "saved file keeps a stable name across edits instead of accumulating copies",
    )


class CreateSessionRequest(request_model(WRITE)):
    """Starts a write session. Every field is the write declaration's."""


class RunSessionRequest(request_model(RUN)):
    """Starts a run session from a freeform task."""


class ReviseSessionRequest(request_model(REVISE)):
    """Starts a revise session from an existing draft."""


class ModelOptions(BaseModel):
    """Choices the UI offers for model/pipeline configuration."""

    stages: list[str] = Field(description="Stage names accepting model overrides")
    suggested_models: list[str] = Field(
        description="Known model ids; free-text ids are also accepted"
    )
    writer_modes: list[str] = Field(description="Available draft production modes")
    default_model: str = Field(description="Active default model")
    default_writer_mode: str = Field(description="Active writer mode")
    default_stage_models: dict[PipelineStage, str] = Field(
        description="Per-stage overrides active in the current settings"
    )


class SessionAction(BaseModel):
    action: Literal["sync", "done", "quit", "feedback"] = Field(
        description="Action to perform"
    )
    text: str | None = Field(default=None, description="Text for feedback action")


class ResumeSessionRequest(request_model(RESUME)):
    """Continues a saved session. Every field is the resume declaration's."""


class RestartSessionRequest(request_model(RESTART)):
    """Regenerates a stage of a saved session onward with fresh agents."""


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
    status: str = "planned"


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


class SessionLaunched(BaseModel):
    """The session a create, resume, or restart request left running."""

    session_id: str = Field(description="Id of the session now running")
    status: SessionStatus = Field(
        default="running", description="The state it was left in"
    )


class ActionAccepted(BaseModel):
    """Confirmation that an action reached its running session."""

    ok: bool = Field(default=True, description="True once the action was delivered")


class ClientMessage(BaseModel):
    """One message the browser sends up a session's socket.

    Which fields carry a value is decided by ``type``, so all of them are
    defaulted and a message that omits the ones its kind does not use still
    validates. Anything else the client sends is ignored rather than refused.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str = Field(default="", description="What the client is asking for")
    items: list[str] = Field(
        default_factory=list, description="Feedback items, for a 'feedback' message"
    )
    text: str | None = Field(
        default=None, description="Free text, for a 'revision' or 'action' message"
    )
    action: str = Field(default="", description="Which action, for an 'action' message")


class SessionEvent(BaseModel):
    """One event a session broadcasts to its clients, and replays from its log.

    The subclasses below are every event a run raises, each declaring the
    ``type`` that names it and the payload that follows; a caller constructs
    one of them rather than assembling a dict. The base declares only what
    every event has, and stays open because it is also what a *stored* event
    validates as: a log written by an older run replays with its own kind and
    fields intact instead of down to nothing.
    """

    model_config = ConfigDict(extra="allow")

    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="When the event was raised, ISO-8601",
    )


class StageEvent(SessionEvent):
    """A pipeline stage began."""

    type: Literal["stage"] = "stage"
    stage: str
    description: str


class ProgressEvent(SessionEvent):
    """A line of narration about what the run is doing."""

    type: Literal["progress"] = "progress"
    message: str


class MessageEvent(SessionEvent):
    """Prose from a named participant — an agent, a subagent, the author."""

    type: Literal["message"] = "message"
    source: str
    message: str


class BlockEvent(SessionEvent):
    """One content block off the agent's stream."""

    type: Literal["block"] = "block"
    block_type: str
    content: str
    prefix: str


class CompleteEvent(SessionEvent):
    """The run finished its article."""

    type: Literal["complete"] = "complete"
    output: CompletionOutput


class CostUpdateEvent(SessionEvent):
    """Refreshed spend for the run so far."""

    type: Literal["cost_update"] = "cost_update"
    cost: CostSnapshot


class StateUpdateEvent(SessionEvent):
    """Refreshed session state — document, title, stage, sections."""

    type: Literal["state_update"] = "state_update"
    state: SessionStateSnapshot


class CollectRevisionEvent(SessionEvent):
    """The run is waiting on the author's revision before it continues."""

    type: Literal["collect_revision"] = "collect_revision"
    state: SessionStateSnapshot


class ErrorEvent(SessionEvent):
    """Something the run needed failed, or the run itself did."""

    type: Literal["error"] = "error"
    message: str


class SessionEndedEvent(SessionEvent):
    """The run stopped, in the state it stopped in."""

    type: Literal["session_ended"] = "session_ended"
    status: SessionStatus


class SessionSummary(BaseModel):
    session_id: str
    title: str = ""
    status: SessionStatus
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
    status: SessionStatus
    state: SessionStateSnapshot
    cost: CostSnapshot
    created_at: str = ""
    profile: str | None = None
    events: list[SerializeAsAny[SessionEvent]] = Field(default_factory=list)
    output: CompletionOutput | None = None
    checkpoints: list[str] = Field(default_factory=list)


class GeneratingPrompt(BaseModel):
    """The original inputs that produced a session, recovered from its snapshot."""

    raw_sources: list[str] = Field(
        default_factory=list,
        description="Source inputs the author supplied: links, file paths, or freeform text",
    )
    author_instructions: str = Field(
        default="", description="Instructions extracted from freeform source text"
    )
    author_deliverables: list[str] = Field(
        default_factory=list,
        description="Deliverables extracted from the author's instructions",
    )
