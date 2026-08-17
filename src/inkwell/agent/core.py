"""Session entry points for the inkwell writing pipeline.

Exports:
- setup_session() — create notes, trace, and session state for a run
- run_session() — unified entry point (fresh run or snapshot resume)
- load_snapshot() — load saved pipeline state for resumption
- SessionTrace — a run's trace, so callers can save it on interrupt
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, ConfigDict

from lup.runtime.usage import CostAccumulator
from lup.types import Usage
from lup.workspace.history import save_session
from lup.telemetry.metrics import (
    get_metrics_summary,
    log_metrics_summary,
    reset_metrics,
)
from lup.workspace.notes import NotesConfig, setup_notes
from lup.workspace.paths import agent_version
from lup.telemetry.trace import TraceLogger

import inkwell.agent.config as config_mod
from inkwell.agent.book import ChapterPlacement
from inkwell.agent.models import AgentSessionResult, PipelineSnapshot
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    DISPLAY_STAGES,
    PipelineError,
    PipelineListener,
    PipelineRunner,
    run_pipeline,
)
from inkwell.agent.session import WritingSessionState

logger = logging.getLogger(__name__)


def notes_path() -> Path:
    return Path(config_mod.current_settings().notes_path)


def traces_path() -> Path:
    return notes_path() / "traces"


class SessionSetup(BaseModel):
    """What setting a session up produced: its notes, its trace, its state."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    notes: NotesConfig
    trace_logger: TraceLogger
    session_state: WritingSessionState


def setup_session(
    session_id: str,
    *,
    session_state: WritingSessionState | None = None,
) -> SessionSetup:
    """Create notes, trace logger, and session state for a writing session."""
    notes = setup_notes(session_id, "0")
    trace_path = traces_path() / session_id / f"{datetime.now().strftime('%H%M%S')}.md"
    trace_logger = TraceLogger(trace_path=trace_path, title=f"Session {session_id}")

    if session_state is None:
        session_state = WritingSessionState()

    return SessionSetup(
        notes=notes,
        trace_logger=trace_logger,
        session_state=session_state,
    )


def pipeline_notes_dir(session_dir: Path) -> Path:
    """Where a session keeps the notes the pipeline writes.

    Named once because three readers need it — the run that writes it, a
    resume that loads its snapshot, and the intake report that inspects it —
    and a path spelled at each of them can be spelled differently.
    """
    return session_dir / "pipeline_notes"


def load_snapshot(
    session_id: str, *, from_stage: str | None = None
) -> PipelineSnapshot | None:
    """Load a saved pipeline snapshot for resumption.

    If from_stage is given, loads the stage-specific snapshot
    (snapshot_{stage}.json) if it exists, otherwise loads the latest
    snapshot and rewinds its stage field.
    """
    from lup.workspace.paths import sessions_dir

    base = pipeline_notes_dir(sessions_dir() / session_id)

    if from_stage:
        stage_path = base / f"snapshot_{from_stage}.json"
        if stage_path.exists():
            return PipelineSnapshot.model_validate_json(
                stage_path.read_text(encoding="utf-8")
            )
        latest_path = base / "snapshot.json"
        if not latest_path.exists():
            return None
        snapshot = PipelineSnapshot.model_validate_json(
            latest_path.read_text(encoding="utf-8")
        )
        snapshot.stage = from_stage
        return snapshot

    snapshot_path = base / "snapshot.json"
    if not snapshot_path.exists():
        return None
    return PipelineSnapshot.model_validate_json(
        snapshot_path.read_text(encoding="utf-8")
    )


def checkpoint_before(session_id: str, redo_stage: str) -> str | None:
    """The latest persisted checkpoint stage strictly before ``redo_stage``.

    Restarting a stage rewinds to the snapshot saved when an earlier stage
    finished, so ``redo_stage`` and everything after it regenerate from clean
    upstream state with fresh agents — that checkpoint carries no SDK session
    ids for the rewound stages, so none of them resume a prior conversation.
    Walks back past stages that never checkpointed (``resolve``, or a stage the
    run never reached). Returns None when nothing earlier was checkpointed.
    """
    from lup.workspace.paths import sessions_dir

    backbone = ["preprocess", *DISPLAY_STAGES]
    if redo_stage not in backbone:
        return None
    base = sessions_dir() / session_id / "pipeline_notes"
    for stage in reversed(backbone[: backbone.index(redo_stage)]):
        if (base / f"snapshot_{stage}.json").exists():
            return stage
    return None


class SessionTrace(BaseModel):
    """A session's trace, handed to :func:`run_session` for it to fill in.

    A caller constructs one before the run and keeps it, so an interrupted
    run still leaves it holding whatever was logged up to the interrupt. It
    is empty until the session opens its log, and saving an empty one writes
    nothing.
    """

    trace_logger: TraceLogger | None = None

    def save(self) -> Path | None:
        """Write the trace out, or nothing when no log was ever opened."""
        return self.trace_logger.save() if self.trace_logger else None


async def run_session(
    *,
    sources: list[str] | None = None,
    refs: list[str] | None = None,
    resume_session_id: str | None = None,
    resume_from_stage: str | None = None,
    restart_from_stage: str | None = None,
    target_format: str = "auto",
    placement: ChapterPlacement | None = None,
    existing_doc_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    listener: PipelineListener | None = None,
    trace: SessionTrace | None = None,
    session_state: WritingSessionState | None = None,
    cost_accumulator: CostAccumulator | None = None,
    stop_after: str | None = None,
    light: bool = False,
    skipped_stages: list[str] | None = None,
) -> AgentSessionResult:
    """Unified entry point for all writing sessions.

    Either sources or resume_session_id must be provided:
    - sources: One or more source materials (URLs, files, share links, freeform text)
    - resume_session_id: Resume from a saved pipeline snapshot
    - restart_from_stage: With resume_session_id, rewind to before this stage and
      regenerate it (and everything after) from scratch with fresh agents,
      discarding their prior output rather than resuming an interrupted agent
    - stop_after: Pause cleanly once this stage finishes (fresh run or resume),
      leaving a checkpoint to resume from after the author reviews the Doc
    - skipped_stages: Backbone stages this run does not perform, read off the
      entry point that launched it rather than decided stage by stage
    - placement: Which chapter of which book this run writes, for a run that is
      one chapter of a book; absent for a standalone piece, which stays
      bookless rather than becoming a book of one chapter
    """
    if session_id is None:
        session_id = resume_session_id or uuid.uuid4().hex[:16]

    active = config_mod.current_settings()
    logger.info(
        "Starting session %s (profile=%s, claude_config_dir=%s)",
        session_id,
        active.profile or "default",
        active.claude_config_dir or "ambient login (server default account)",
    )
    reset_metrics()

    setup = setup_session(session_id, session_state=session_state)

    if trace is not None:
        trace.trace_logger = setup.trace_logger

    cost_acc = cost_accumulator or CostAccumulator()
    pipeline_notes = PipelineNotes(pipeline_notes_dir(setup.notes.session))

    try:
        if resume_session_id:
            if restart_from_stage:
                checkpoint = checkpoint_before(resume_session_id, restart_from_stage)
                if checkpoint is None:
                    raise PipelineError(
                        f"Cannot restart from '{restart_from_stage}': no earlier "
                        "checkpoint to rewind to."
                    )
                snapshot = load_snapshot(resume_session_id, from_stage=checkpoint)
            else:
                snapshot = load_snapshot(
                    resume_session_id, from_stage=resume_from_stage
                )
            if snapshot is None:
                raise PipelineError(
                    f"No snapshot found for session '{resume_session_id}'. "
                    "Cannot resume without saved pipeline state."
                )
            if snapshot.doc_id:
                existing_doc_id = snapshot.doc_id
            elif snapshot.output and snapshot.output.google_doc_id:
                existing_doc_id = snapshot.output.google_doc_id

            runner = PipelineRunner(
                sources=sources or [],
                refs=refs or [],
                target_format=target_format,
                placement=placement,
                existing_doc_id=existing_doc_id,
                session_state=setup.session_state,
                notes=pipeline_notes,
                trace_logger=setup.trace_logger,
                listener=listener,
                cost_accumulator=cost_acc,
                stop_after=stop_after,
                light=light,
                skipped_stages=skipped_stages or [],
            )
            output = await runner.run_from(snapshot, restart=bool(restart_from_stage))
        else:
            output = await run_pipeline(
                sources=sources or [],
                refs=refs or [],
                target_format=target_format,
                placement=placement,
                existing_doc_id=existing_doc_id,
                session_state=setup.session_state,
                notes=pipeline_notes,
                trace_logger=setup.trace_logger,
                listener=listener,
                cost_accumulator=cost_acc,
                stop_after=stop_after,
                light=light,
                skipped_stages=skipped_stages or [],
            )
    finally:
        setup.trace_logger.save()

    if cost_acc.stages:
        output.stage_costs = {
            name: {
                "cost_usd": sc.cost_usd,
                "duration_s": sc.duration.total_seconds(),
                "input_tokens": sc.input_tokens,
                "output_tokens": sc.output_tokens,
                "calls": sc.turn_count,
            }
            for name, sc in cost_acc.stages.items()
        }

    log_metrics_summary()

    result = AgentSessionResult(
        session_id=session_id,
        task_id=task_id,
        agent_version=agent_version(),
        timestamp=datetime.now().isoformat(),
        output=output,
        reasoning="",
        sources_consulted=[],
        duration_seconds=(
            cost_acc.total.duration.total_seconds()
            if cost_acc.total.turn_count
            else None
        ),
        cost_usd=cost_acc.total.cost_usd if cost_acc.total.turn_count else None,
        token_usage=Usage(
            input_tokens=cost_acc.total.input_tokens,
            output_tokens=cost_acc.total.output_tokens,
        )
        if cost_acc.total.turn_count
        else None,
        tool_metrics=get_metrics_summary(),
        profile=config_mod.current_settings().profile,
    )

    save_session(result, session_id=result.session_id)
    return result
