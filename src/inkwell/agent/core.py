"""Session entry points for the inkwell writing pipeline.

Exports:
- setup_session() — create notes, trace, and session state for a run
- run_session() — unified entry point (fresh run or snapshot resume)
- load_snapshot() — load saved pipeline state for resumption
- SessionTrace — trace handle so callers can save on interrupt
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from lup.client import CostAccumulator, TokenUsage
from lup.history import save_session
from lup.metrics import get_metrics_summary, log_metrics_summary, reset_metrics
from lup.notes import NotesConfig, setup_notes
from lup.paths import agent_version
from lup.trace import TraceLogger

import inkwell.agent.config as config_mod
from inkwell.agent.models import AgentSessionResult, PipelineSnapshot
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
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


class SessionSetup(NamedTuple):
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


def load_snapshot(
    session_id: str, *, from_stage: str | None = None
) -> PipelineSnapshot | None:
    """Load a saved pipeline snapshot for resumption.

    If from_stage is given, loads the stage-specific snapshot
    (snapshot_{stage}.json) if it exists, otherwise loads the latest
    snapshot and rewinds its stage field.
    """
    from lup.paths import sessions_dir

    base = sessions_dir() / session_id / "pipeline_notes"

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


class SessionTrace:
    """Holds trace state so callers can save on interrupt."""

    def __init__(self, trace_logger: TraceLogger) -> None:
        self.trace_logger = trace_logger

    def save(self) -> Path:
        return self.trace_logger.save()


async def run_session(
    *,
    sources: list[str] | None = None,
    refs: list[str] | None = None,
    resume_session_id: str | None = None,
    resume_from_stage: str | None = None,
    target_format: str = "auto",
    existing_doc_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    listener: PipelineListener | None = None,
    trace_holder: list[SessionTrace] | None = None,
    session_state: WritingSessionState | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> AgentSessionResult:
    """Unified entry point for all writing sessions.

    Either sources or resume_session_id must be provided:
    - sources: One or more source materials (URLs, files, share links, freeform text)
    - resume_session_id: Resume from a saved pipeline snapshot
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

    if trace_holder is not None:
        trace_holder.append(SessionTrace(setup.trace_logger))

    cost_acc = cost_accumulator or CostAccumulator()
    pipeline_notes = PipelineNotes(setup.notes.session / "pipeline_notes")

    try:
        if resume_session_id:
            snapshot = load_snapshot(resume_session_id, from_stage=resume_from_stage)
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
                existing_doc_id=existing_doc_id,
                session_state=setup.session_state,
                notes=pipeline_notes,
                trace_logger=setup.trace_logger,
                listener=listener,
                cost_accumulator=cost_acc,
            )
            output = await runner.run_from(snapshot)
        else:
            output = await run_pipeline(
                sources=sources or [],
                refs=refs or [],
                target_format=target_format,
                existing_doc_id=existing_doc_id,
                session_state=setup.session_state,
                notes=pipeline_notes,
                trace_logger=setup.trace_logger,
                listener=listener,
                cost_accumulator=cost_acc,
            )
    finally:
        setup.trace_logger.save()

    if cost_acc.stages:
        output.stage_costs = {
            name: {
                "cost_usd": sc.cost_usd,
                "duration_s": sc.duration_seconds,
                "input_tokens": sc.input_tokens,
                "output_tokens": sc.output_tokens,
                "calls": sc.call_count,
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
        duration_seconds=cost_acc.duration_seconds if cost_acc.call_count else None,
        cost_usd=cost_acc.total_cost_usd if cost_acc.call_count else None,
        token_usage=TokenUsage(
            input_tokens=cost_acc.total_input_tokens,
            output_tokens=cost_acc.total_output_tokens,
        )
        if cost_acc.call_count
        else None,
        tool_metrics=get_metrics_summary(),
        profile=config_mod.current_settings().profile,
    )

    save_session(result, session_id=result.session_id)
    return result
