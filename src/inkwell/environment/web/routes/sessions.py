"""REST endpoints for session management."""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from inkwell.agent.stages import OUTPUT_FORMATS
from inkwell.environment.entrypoints import (
    RESUMED_SESSION,
    EntryPointDescriptor,
    EntryPointValues,
    SuppliedValues,
    entry_point_descriptors,
)
from inkwell.environment.web.models import (
    ActionAccepted,
    ChapterSessionRequest,
    CreateSessionRequest,
    FormatOption,
    GeneratingPrompt,
    ModelOptions,
    RestartSessionRequest,
    ResumeSessionRequest,
    ReviseSessionRequest,
    RunSessionRequest,
    SessionAction,
    SessionDetail,
    SessionLaunched,
    SessionSummary,
    UploadResult,
    UploadTextRequest,
)
from inkwell.environment.web.session_manager import ManagerHolder, SessionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
formats_router = APIRouter(prefix="/api", tags=["config"])

holder = ManagerHolder()
"""Filled in at app startup, so a route reaches the manager without a global."""


def set_manager(m: SessionManager) -> None:
    holder.manager = m


def get_manager() -> SessionManager:
    return holder.require()


def summarize_source(value: str) -> str:
    """A short, log-safe description of one source/ref input.

    Distinguishes an uploaded file path from a URL from freeform text and
    previews it, so a dropped attachment is obvious in the log without dumping
    a whole pasted document.
    """
    stripped = value.strip()
    path = Path(stripped)
    if path.is_file():
        return f"file:{path} ({path.stat().st_size} B)"
    if stripped.startswith(("http://", "https://")):
        return f"url:{stripped[:80]}"
    return f"text[{len(stripped)}]:{stripped[:60]!r}"


@formats_router.get("/formats")
async def get_formats() -> list[FormatOption]:
    auto = FormatOption(
        key="auto",
        label="Auto — agent picks the best format",
        accepts_description=False,
    )
    return [
        auto,
        *(
            FormatOption(
                key=f.key, label=f.label, accepts_description=f.accepts_description
            )
            for f in OUTPUT_FORMATS
        ),
    ]


@formats_router.get("/model-options")
async def get_model_options() -> ModelOptions:
    import inkwell.agent.config as config_mod
    from inkwell.agent.config import PIPELINE_STAGES, SUGGESTED_MODELS, WRITER_MODES

    settings = config_mod.settings
    return ModelOptions(
        stages=list(PIPELINE_STAGES),
        suggested_models=list(SUGGESTED_MODELS),
        writer_modes=list(WRITER_MODES),
        default_model=settings.model,
        default_writer_mode=settings.writer_mode,
        default_stage_models=dict(settings.stage_models),
    )


@formats_router.get("/pipeline-stages")
async def get_pipeline_stages() -> list[str]:
    """The ordered author-facing stage backbone the progress bar renders.

    Served from the pipeline's own ``DISPLAY_STAGES`` so the UI mirrors the
    real stage sequence instead of a hand-maintained copy that drifts.
    """
    from inkwell.agent.pipeline import DISPLAY_STAGES

    return list(DISPLAY_STAGES)


@formats_router.get("/entry-points")
async def get_entry_points() -> list[EntryPointDescriptor]:
    """Every way a session starts, and the parameters each one takes.

    Rendered off the entry point declaration the same way ``/api/formats`` is
    rendered off ``OUTPUT_FORMATS``, so the New Session form builds its controls
    from what the declaration says rather than from a TypeScript copy of it that
    can fall behind.
    """
    return entry_point_descriptors()


@formats_router.get("/stop-stages")
async def get_stop_stages() -> list[str]:
    """Stages a run can be paused after — the valid ``stop_after`` targets.

    Served from the pipeline's ``CHECKPOINT_STAGES`` (the backbone minus stages
    that never checkpoint), so the New Session stop-after picker offers only
    stages the run can actually pause and resume from.
    """
    from inkwell.agent.pipeline import CHECKPOINT_STAGES

    return list(CHECKPOINT_STAGES)


def collected(entry_point: str, body: SuppliedValues) -> EntryPointValues:
    """One request body as the declared values it supplies.

    Every route funnels through here, so a parameter added to a declaration
    reaches the manager without an argument being threaded through a route.
    A value the declaration refuses raises ``ValueError``, which the routes
    report as a bad request.
    """
    return EntryPointValues.declared(entry_point, body)


async def launch_fresh(entry_point: str, body: SuppliedValues) -> SessionLaunched:
    """Start one of the fresh entry points from the body it was posted."""
    try:
        values = collected(entry_point, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sources = values.declaration.sources(values)
    logger.info(
        "%s: %d source(s)=%s",
        entry_point,
        len(sources),
        [summarize_source(s) for s in sources],
    )
    session_id = await get_manager().create_session(values)
    return SessionLaunched(session_id=session_id)


async def launch_continued(
    entry_point: str, session_id: str, body: SuppliedValues
) -> SessionLaunched:
    """Continue a saved session, resuming it or restarting a stage of it.

    The session is the resource the path names rather than a body field, so it is
    supplied under its declared name here and read back off the declaration like
    any other parameter.
    """
    try:
        values = collected(entry_point, {**body, RESUMED_SESSION.name: session_id})
        sid = await get_manager().continue_session(values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SessionLaunched(session_id=sid)


@router.post("", status_code=201)
@router.post("/write", status_code=201)
async def create_session(req: CreateSessionRequest) -> SessionLaunched:
    """Start a write session, at the entry point's own path and at the collection's.

    Every fresh entry point answers at ``/api/sessions/<name>``, so the browser
    addresses the one the author picked without a table mapping names to paths;
    the bare collection path is where a write session has always been posted.
    """
    return await launch_fresh("write", req.model_dump(mode="json"))


@router.post("/run", status_code=201)
async def run_session_from_task(req: RunSessionRequest) -> SessionLaunched:
    return await launch_fresh("run", req.model_dump(mode="json"))


@router.post("/revise", status_code=201)
async def revise_session(req: ReviseSessionRequest) -> SessionLaunched:
    return await launch_fresh("revise", req.model_dump(mode="json"))


@router.post("/chapter", status_code=201)
async def chapter_session(req: ChapterSessionRequest) -> SessionLaunched:
    return await launch_fresh("chapter", req.model_dump(mode="json"))


@router.get("")
async def list_sessions() -> list[SessionSummary]:
    return get_manager().list_sessions()


@router.get("/{session_id}")
async def get_session(session_id: str) -> SessionDetail:
    detail = get_manager().get_session_detail(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return detail


@router.get("/{session_id}/prompt")
async def get_session_prompt(session_id: str) -> GeneratingPrompt:
    prompt = get_manager().get_generating_prompt(session_id)
    if prompt is None:
        raise HTTPException(
            status_code=404, detail="No source prompt recorded for this session"
        )
    return prompt


@router.post("/{session_id}/resume")
async def resume_session(
    session_id: str, req: ResumeSessionRequest | None = None
) -> SessionLaunched:
    body = req.model_dump(mode="json") if req else {}
    return await launch_continued("resume", session_id, body)


@router.post("/{session_id}/restart")
async def restart_session(
    session_id: str, req: RestartSessionRequest
) -> SessionLaunched:
    return await launch_continued("restart", session_id, req.model_dump(mode="json"))


UPLOAD_DIR = Path(tempfile.gettempdir()) / "inkwell_uploads"
ALLOWED_EXTENSIONS = {".pdf", ".md", ".txt", ".html", ".htm"}
TEXT_EXTENSIONS = {".md", ".txt", ".html", ".htm"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


@router.post("/upload", status_code=201)
async def upload_file(file: UploadFile) -> UploadResult:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {suffix}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 100MB)")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / file.filename
    counter = 1
    while dest.exists():
        dest = UPLOAD_DIR / f"{Path(file.filename).stem}_{counter}{suffix}"
        counter += 1
    dest.write_bytes(content)
    logger.info("upload: %s (%d B) -> %s", file.filename, len(content), dest)
    return UploadResult(path=str(dest), filename=dest.name, size=len(content))


def coerce_text_filename(name: str) -> str:
    """Force a pasted document's name to a supported text extension.

    Pasted content is always text, so a missing or non-text suffix (the user
    typed a bare name, or a binary extension that can't apply to text) becomes
    ``.md`` — keeping the file readable by the same extractor that ingests
    uploaded ``.md``/``.txt``/``.html`` documents.
    """
    path = Path(name.strip())
    stem = path.stem or "pasted"
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        suffix = ".md"
    return f"{stem}{suffix}"


def free_upload_path(filename: str) -> Path:
    """A non-colliding path under the upload dir, suffixing on collision."""
    dest = UPLOAD_DIR / filename
    counter = 1
    while dest.exists():
        dest = UPLOAD_DIR / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1
    return dest


@router.post("/upload-text", status_code=201)
async def upload_text(req: UploadTextRequest) -> UploadResult:
    """Save pasted/edited text as an upload, usable as a source or reference.

    Lets the author turn text they have in hand into a named document without
    the round trip of saving a local file first. An edit passes the previous
    upload as ``replace_path``: that file is dropped before the rewrite so the
    name is free to reuse, giving a stable filename across edits and leaving no
    orphan copies behind.
    """
    raw = req.content.encode("utf-8")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Text too large (max 100MB)")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if req.replace_path:
        old = Path(req.replace_path).resolve()
        if UPLOAD_DIR.resolve() in old.parents and old.is_file():
            old.unlink()

    dest = free_upload_path(coerce_text_filename(req.filename))
    dest.write_text(req.content, encoding="utf-8")
    logger.info(
        "upload-text: %r (%d B) -> %s (replace=%s)",
        req.filename,
        len(raw),
        dest,
        req.replace_path,
    )
    return UploadResult(path=str(dest), filename=dest.name, size=len(raw))


@router.post("/{session_id}/action")
async def session_action(session_id: str, req: SessionAction) -> ActionAccepted:
    ok = await get_manager().send_action(session_id, req.action, req.text)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or not running")
    return ActionAccepted()
