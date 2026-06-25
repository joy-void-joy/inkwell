"""REST endpoints for session management."""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from inkwell.agent.stages import OUTPUT_FORMATS
from inkwell.environment.web.models import (
    CreateSessionRequest,
    FormatOption,
    GeneratingPrompt,
    ModelOptions,
    RestartSessionRequest,
    ResumeSessionRequest,
    SessionAction,
    SessionDetail,
    SessionSummary,
    UploadResult,
    UploadTextRequest,
)
from inkwell.environment.web.session_manager import SessionManager

logger = logging.getLogger(__name__)

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


@formats_router.get("/stop-stages")
async def get_stop_stages() -> list[str]:
    """Stages a run can be paused after — the valid ``stop_after`` targets.

    Served from the pipeline's ``CHECKPOINT_STAGES`` (the backbone minus stages
    that never checkpoint), so the New Session stop-after picker offers only
    stages the run can actually pause and resume from.
    """
    from inkwell.agent.pipeline import CHECKPOINT_STAGES

    return list(CHECKPOINT_STAGES)


@router.post("", status_code=201)
async def create_session(req: CreateSessionRequest) -> dict[str, str]:
    mgr = get_manager()
    logger.info(
        "create_session: %d source(s)=%s refs=%s format=%s",
        len(req.sources),
        [summarize_source(s) for s in req.sources],
        [summarize_source(r) for r in (req.refs or [])],
        req.target_format,
    )
    session_id = await mgr.create_session(
        sources=req.sources,
        refs=req.refs or None,
        target_format=req.target_format,
        existing_doc_id=req.existing_doc_id,
        profile=req.profile,
        model=req.model,
        stage_models=req.stage_models,
        writer_mode=req.writer_mode,
        stop_after=req.stop_after,
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
            stop_after=req.stop_after if req else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": sid, "status": "running"}


@router.post("/{session_id}/restart")
async def restart_session(
    session_id: str, req: RestartSessionRequest
) -> dict[str, str]:
    mgr = get_manager()
    try:
        sid = await mgr.restart_session(
            session_id,
            from_stage=req.from_stage,
            profile=req.profile,
            model=req.model,
            stage_models=req.stage_models,
            writer_mode=req.writer_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": sid, "status": "running"}


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
async def session_action(session_id: str, req: SessionAction) -> dict[str, bool]:
    ok = await get_manager().send_action(session_id, req.action, req.text)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or not running")
    return {"ok": True}
