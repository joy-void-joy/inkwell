"""REST endpoints for session management."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from inkwell.agent.stages import OUTPUT_FORMATS
from inkwell.environment.web.models import (
    CreateSessionRequest,
    FormatOption,
    GeneratingPrompt,
    ModelOptions,
    ResumeSessionRequest,
    SessionAction,
    SessionDetail,
    SessionSummary,
    UploadResult,
)
from inkwell.environment.web.session_manager import SessionManager

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
formats_router = APIRouter(prefix="/api", tags=["config"])

manager: SessionManager | None = None


def set_manager(m: SessionManager) -> None:
    global manager  # noqa: PLW0603
    manager = m


def get_manager() -> SessionManager:
    if manager is None:
        raise RuntimeError("SessionManager not initialized")
    return manager


@formats_router.get("/formats")
async def get_formats() -> list[FormatOption]:
    return [
        FormatOption(
            key=f.key, label=f.label, accepts_description=f.accepts_description
        )
        for f in OUTPUT_FORMATS
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


@router.post("", status_code=201)
async def create_session(req: CreateSessionRequest) -> dict[str, str]:
    mgr = get_manager()
    session_id = await mgr.create_session(
        sources=req.sources,
        refs=req.refs or None,
        target_format=req.target_format,
        existing_doc_id=req.existing_doc_id,
        profile=req.profile,
        model=req.model,
        stage_models=req.stage_models,
        writer_mode=req.writer_mode,
    )
    return {"session_id": session_id, "status": "running"}


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
) -> dict[str, str]:
    mgr = get_manager()
    from_stage = req.from_stage if req else None
    profile = req.profile if req else None
    try:
        sid = await mgr.resume_session(
            session_id,
            from_stage=from_stage,
            profile=profile,
            model=req.model if req else None,
            stage_models=req.stage_models if req else None,
            writer_mode=req.writer_mode if req else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": sid, "status": "running"}


UPLOAD_DIR = Path(tempfile.gettempdir()) / "inkwell_uploads"
ALLOWED_EXTENSIONS = {".pdf", ".md", ".txt", ".html", ".htm"}
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
    return UploadResult(path=str(dest), filename=dest.name, size=len(content))


@router.post("/{session_id}/action")
async def session_action(session_id: str, req: SessionAction) -> dict[str, bool]:
    ok = await get_manager().send_action(session_id, req.action, req.text)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or not running")
    return {"ok": True}
