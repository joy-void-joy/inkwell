"""The one path from declared entry point values to a running pipeline.

Both surfaces run through here. The CLI's chat session and the web session
manager each hand over the values their surface collected, and this reads every
parameter off the declaration in :mod:`inkwell.environment.entrypoints` and hands
the pipeline what it asks for. Because it is the only place the declared set is
unpacked, a parameter the pipeline already understands reaches both surfaces
without being threaded through either — and a profile or model override is
applied once rather than once per environment.
"""

from inkwell.agent.client import CostAccumulator
from inkwell.agent.config import Settings, active_profile, load_settings, use_settings
from inkwell.agent.core import SessionTrace, run_session
from inkwell.agent.models import AgentSessionResult
from inkwell.agent.pipeline import PipelineListener
from inkwell.agent.session import WritingSessionState
from inkwell.environment.entrypoints import (
    CHAPTER,
    EXISTING_DOC_ID,
    LIGHT,
    MODEL,
    PROFILE,
    REFS,
    STAGE_MODELS,
    STOP_AFTER,
    TARGET_FORMAT,
    WRITER_MODE,
    EntryPointValues,
)


def declared_profile(values: EntryPointValues) -> str | None:
    """Which profile a run bills, in the order a surface can supply one.

    A profile the values carry wins; then the one the root ``--profile`` option
    selected for this process; then, for a continued run, the profile its
    snapshot recorded, so resuming bills the account the run started on.
    """
    explicit = PROFILE.read(values)
    if explicit:
        return explicit
    selected = active_profile()
    if selected:
        return selected
    entry_point = values.declaration
    resumed = entry_point.resumed_session(values)
    if not resumed:
        return None
    from inkwell.agent.core import load_snapshot

    snapshot = load_snapshot(resumed, from_stage=entry_point.resume_from(values))
    return snapshot.profile if snapshot else None


def session_settings(values: EntryPointValues) -> Settings:
    """The settings one run uses: its profile's, with the declared overrides on top."""
    resolved = load_settings(declared_profile(values))
    model = MODEL.read(values)
    if model:
        resolved.model = model
    stage_models = STAGE_MODELS.read(values)
    if stage_models:
        resolved.stage_models = {**resolved.stage_models, **stage_models}
    writer_mode = WRITER_MODE.read(values)
    if writer_mode:
        resolved.writer_mode = writer_mode
    return resolved


async def run_declared_session(
    values: EntryPointValues,
    *,
    session_id: str,
    listener: PipelineListener | None = None,
    trace: SessionTrace | None = None,
    session_state: WritingSessionState | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> AgentSessionResult:
    """Run one declared entry point, reading each parameter off the declaration.

    The keyword arguments are the run's collaborators — who watches it, where its
    trace and its spend go — which no surface declares and no author supplies.
    Everything the author did supply is read from ``values``.

    The declared profile and model overrides are scoped to this task, so
    concurrent sessions read their own configuration and each spawned ``claude``
    CLI bills the account its profile names.
    """
    entry_point = values.declaration
    with use_settings(session_settings(values)):
        return await run_session(
            sources=entry_point.sources(values),
            refs=REFS.read(values),
            resume_session_id=entry_point.resumed_session(values),
            resume_from_stage=entry_point.resume_from(values),
            restart_from_stage=entry_point.restart_from(values),
            target_format=TARGET_FORMAT.read(values),
            placement=CHAPTER.read(values),
            existing_doc_id=EXISTING_DOC_ID.read(values),
            session_id=session_id,
            listener=listener,
            trace=trace,
            session_state=session_state,
            cost_accumulator=cost_accumulator,
            stop_after=STOP_AFTER.read(values),
            light=LIGHT.read(values),
            skipped_stages=entry_point.skipped_stages,
        )
