"""Session lifecycle management for the web environment."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import WebSocket

from inkwell.agent.client import CostAccumulator, is_interrupt
from lup.workspace.history import latest_session_record, list_all_session_ids
from lup.workspace.paths import sessions_dir, trace_logs_dir

from inkwell.agent.core import SessionTrace, run_session
from inkwell.agent.google_auth import GoogleAuthError
from inkwell.agent.models import AgentSessionResult, PipelineSnapshot
from inkwell.agent.pipeline import PipelineInterrupted
from inkwell.agent.session import WritingSessionState
from inkwell.environment.web.listener import WebListener
from inkwell.environment.web.models import (
    CompletionOutput,
    CostSnapshot,
    GeneratingPrompt,
    HistorySessionData,
    SectionInfo,
    SessionDetail,
    SessionStateSnapshot,
    SessionSummary,
    StageCostSummary,
)

logger = logging.getLogger(__name__)


def classify_session_failure(exc: BaseException) -> tuple[str, str]:
    """Map a failed pipeline task to a ``(status, client_message)``.

    An interrupt is resumable, not a failure: a resume's ``PipelineInterrupted``
    names the stage it stopped at, and a SIGINT-killed subprocess (a reloaded
    dev server, a Ctrl-C) surfaces through ``is_interrupt``. Both become
    ``"interrupted"`` so the UI invites a resume instead of reporting a crash.
    """
    stage = exc.stage if isinstance(exc, PipelineInterrupted) else None
    if stage is not None or is_interrupt(exc):
        detail = f" during {stage}" if stage else ""
        return "interrupted", f"Session interrupted{detail}. Resume to continue."
    return "failed", str(exc)


@dataclass
class SessionHandle:
    session_id: str
    task: asyncio.Task[AgentSessionResult]
    state: WritingSessionState
    cost: CostAccumulator
    listener: WebListener
    profile: str | None = None
    sources: list[str] = field(default_factory=list)
    trace_holder: list[SessionTrace] = field(default_factory=list)
    clients: set[WebSocket] = field(default_factory=set)
    status: str = "running"
    result: AgentSessionResult | None = None
    created_at: datetime = field(default_factory=datetime.now)
    events: list[dict[str, object]] = field(default_factory=list)


class SessionManager:
    """Manages running pipeline sessions and WebSocket connections."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionHandle] = {}

    async def create_session(
        self,
        sources: list[str],
        *,
        refs: list[str] | None = None,
        target_format: str = "auto",
        existing_doc_id: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        stage_models: dict[str, str] | None = None,
        writer_mode: str | None = None,
        stop_after: str | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex[:16]
        return self.launch(
            session_id=session_id,
            sources=sources,
            refs=refs,
            target_format=target_format,
            existing_doc_id=existing_doc_id,
            profile=profile,
            model=model,
            stage_models=stage_models,
            writer_mode=writer_mode,
            stop_after=stop_after,
        )

    async def resume_session(
        self,
        resume_session_id: str,
        *,
        from_stage: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        stage_models: dict[str, str] | None = None,
        writer_mode: str | None = None,
        stop_after: str | None = None,
    ) -> str:
        if resume_session_id in self.sessions:
            handle = self.sessions[resume_session_id]
            if handle.status == "running":
                return resume_session_id

        return self.launch(
            session_id=resume_session_id,
            resume_session_id=resume_session_id,
            resume_from_stage=from_stage,
            profile=self.resolve_session_profile(
                resume_session_id, profile, from_stage
            ),
            model=model,
            stage_models=stage_models,
            writer_mode=writer_mode,
            stop_after=stop_after,
        )

    async def restart_session(
        self,
        resume_session_id: str,
        *,
        from_stage: str,
        profile: str | None = None,
        model: str | None = None,
        stage_models: dict[str, str] | None = None,
        writer_mode: str | None = None,
    ) -> str:
        """Rewind to before ``from_stage`` and regenerate it (and everything
        after) from scratch with fresh agents — unlike resume, which continues
        an interrupted agent's own conversation."""
        if resume_session_id in self.sessions:
            handle = self.sessions[resume_session_id]
            if handle.status == "running":
                return resume_session_id

        return self.launch(
            session_id=resume_session_id,
            resume_session_id=resume_session_id,
            restart_from_stage=from_stage,
            profile=self.resolve_session_profile(resume_session_id, profile, None),
            model=model,
            stage_models=stage_models,
            writer_mode=writer_mode,
        )

    def resolve_session_profile(
        self, resume_session_id: str, profile: str | None, from_stage: str | None
    ) -> str:
        """Pin the profile whose account is billed for a resumed/restarted run.

        Prefer an explicit override, then a live handle, then the snapshot's
        recorded profile. Raises if none is known, since the spawned claude CLI
        must bill a specific account.
        """
        if profile:
            return profile
        original = self.sessions.get(resume_session_id)
        if original and original.profile:
            return original.profile
        from inkwell.agent.core import load_snapshot

        snapshot = load_snapshot(resume_session_id, from_stage=from_stage)
        if snapshot and snapshot.profile:
            return snapshot.profile
        raise ValueError(
            "Cannot resume: no profile found. The session predates profile "
            "tracking, or the snapshot is missing. Please specify a profile."
        )

    def launch(
        self,
        *,
        session_id: str,
        sources: list[str] | None = None,
        refs: list[str] | None = None,
        target_format: str = "auto",
        existing_doc_id: str | None = None,
        resume_session_id: str | None = None,
        resume_from_stage: str | None = None,
        restart_from_stage: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        stage_models: dict[str, str] | None = None,
        writer_mode: str | None = None,
        stop_after: str | None = None,
    ) -> str:
        from inkwell.agent.config import load_settings, use_settings

        session_settings = load_settings(profile)
        if model:
            session_settings.model = model
        if stage_models:
            session_settings.stage_models = {
                **session_settings.stage_models,
                **stage_models,
            }
        if writer_mode:
            session_settings.writer_mode = writer_mode

        state = WritingSessionState()
        cost = CostAccumulator()
        listener = WebListener(session_id, self)
        trace_holder: list[SessionTrace] = []

        async def run_in_session_context() -> AgentSessionResult:
            # Each task owns a contextvar copy, so this scopes the
            # configuration to this session and everything it spawns —
            # concurrent sessions with different profiles don't race, and
            # the spawned claude CLI bills the profile's account.
            with use_settings(session_settings):
                return await run_session(
                    sources=sources or [],
                    refs=refs,
                    target_format=target_format,
                    existing_doc_id=existing_doc_id,
                    resume_session_id=resume_session_id,
                    resume_from_stage=resume_from_stage,
                    restart_from_stage=restart_from_stage,
                    session_id=session_id,
                    listener=listener,
                    session_state=state,
                    cost_accumulator=cost,
                    trace_holder=trace_holder,
                    stop_after=stop_after,
                )

        task = asyncio.create_task(
            run_in_session_context(),
            name=f"inkwell-session-{session_id}",
        )

        handle = SessionHandle(
            session_id=session_id,
            task=task,
            state=state,
            cost=cost,
            listener=listener,
            profile=profile,
            sources=sources or [],
            trace_holder=trace_holder,
        )
        if resume_session_id:
            handle.events = self.load_events(resume_session_id)
        self.sessions[session_id] = handle

        task.add_done_callback(
            lambda t: asyncio.create_task(self.on_session_done(session_id, t))
        )

        return session_id

    async def on_session_done(
        self, session_id: str, task: asyncio.Task[AgentSessionResult]
    ) -> None:
        handle = self.sessions.get(session_id)
        if not handle:
            return

        try:
            handle.result = task.result()
            output = handle.result.output
            # A run launched with a stop point returns normally but carries a
            # paused marker instead of finished content — it is resumable, not
            # done, so the UI must show it as paused rather than completed.
            handle.status = (
                "paused" if output is not None and output.paused_after else "completed"
            )
            await self.broadcast(
                session_id,
                {
                    "type": "session_ended",
                    "status": handle.status,
                    "timestamp": datetime.now().isoformat(),
                },
            )
        except asyncio.CancelledError:
            handle.status = "cancelled"
            await self.broadcast(
                session_id,
                {
                    "type": "session_ended",
                    "status": "cancelled",
                    "timestamp": datetime.now().isoformat(),
                },
            )
        except GoogleAuthError:
            handle.status = "failed"
            profile = handle.profile or "default"
            logger.warning(
                "Session %s halted: Google auth expired for profile %s",
                session_id,
                profile,
            )
            message = (
                f"Google access for the '{profile}' profile has expired or been "
                "revoked. Re-authorize it under Settings → Profiles → "
                "Google, then resume this session."
            )
            await self.broadcast(
                session_id,
                {
                    "type": "error",
                    "message": message,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            await self.broadcast(
                session_id,
                {
                    "type": "session_ended",
                    "status": "failed",
                    "timestamp": datetime.now().isoformat(),
                },
            )
        except (
            Exception
        ) as exc:  # claude: ignore — SDK raises generic Exception for exit codes
            ended_status, message = classify_session_failure(exc)
            handle.status = ended_status
            if ended_status == "failed":
                logger.exception("Session %s failed", session_id)
            else:
                logger.info("Session %s interrupted — resumable", session_id)
            await self.broadcast(
                session_id,
                {
                    "type": "error",
                    "message": message,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            await self.broadcast(
                session_id,
                {
                    "type": "session_ended",
                    "status": ended_status,
                    "timestamp": datetime.now().isoformat(),
                },
            )
        finally:
            if handle.trace_holder:
                try:
                    trace_path = handle.trace_holder[0].save()
                    logger.info("Trace saved to %s", trace_path)
                except (OSError, RuntimeError):
                    logger.warning(
                        "Failed to save trace for session %s", session_id, exc_info=True
                    )
            self.save_events(handle)

    def save_events(self, handle: SessionHandle) -> None:
        try:
            log_dir = trace_logs_dir() / handle.session_id
            log_dir.mkdir(parents=True, exist_ok=True)
            events_file = log_dir / "events.json"
            events_file.write_text(
                json.dumps(handle.events, default=str), encoding="utf-8"
            )
        except OSError:
            logger.warning("Failed to save events for session %s", handle.session_id)

    def load_events(self, session_id: str) -> list[dict[str, object]]:  # claude: ignore
        log_dir = trace_logs_dir() / session_id
        events_file = log_dir / "events.json"
        if not events_file.exists():
            return []
        try:
            raw: object = json.loads(events_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return raw
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load events for session %s", session_id)
        return []

    def load_snapshot(self, session_id: str) -> PipelineSnapshot | None:
        from inkwell.agent.core import load_snapshot

        return load_snapshot(session_id)

    def get_generating_prompt(self, session_id: str) -> GeneratingPrompt | None:
        snapshot = self.load_snapshot(session_id)
        if snapshot and (snapshot.raw_sources or snapshot.author_instructions):
            return GeneratingPrompt(
                raw_sources=snapshot.raw_sources,
                author_instructions=snapshot.author_instructions,
                author_deliverables=snapshot.author_deliverables,
            )
        handle = self.sessions.get(session_id)
        if handle and handle.sources:
            return GeneratingPrompt(raw_sources=list(handle.sources))
        return None

    def snapshot_checkpoints(self, session_id: str) -> list[str]:
        notes_dir = sessions_dir() / session_id / "pipeline_notes"
        if not notes_dir.exists():
            return []
        return sorted(
            f.stem.removeprefix("snapshot_") for f in notes_dir.glob("snapshot_*.json")
        )

    def list_sessions(self) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        seen_ids: set[str] = set()

        for handle in self.sessions.values():
            seen_ids.add(handle.session_id)
            title = handle.result.output.title if handle.result else handle.state.title
            summaries.append(
                SessionSummary(
                    session_id=handle.session_id,
                    title=title,
                    status=handle.status,  # type: ignore[arg-type]
                    stage=handle.state.stage,
                    cost_usd=handle.cost.total.cost_usd,
                    duration_s=handle.cost.total.duration.total_seconds(),
                    doc_url=handle.state.doc_url,
                    created_at=handle.created_at.isoformat(),
                    profile=handle.profile,
                )
            )

        for sid in list_all_session_ids():
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            record = latest_session_record(sid)
            if record is None:
                snapshot = self.load_snapshot(sid)
                if snapshot and snapshot.stage != "init":
                    title = snapshot.plan.title if snapshot.plan else ""
                    summaries.append(
                        SessionSummary(
                            session_id=sid,
                            title=title,
                            status="interrupted",
                            stage=snapshot.stage,
                            doc_url=snapshot.doc_url,
                            profile=snapshot.profile,
                            checkpoints=self.snapshot_checkpoints(sid),
                        )
                    )
                continue
            parsed = HistorySessionData.model_validate(record.model_dump())
            paused_stage = parsed.output.paused_after if parsed.output else ""
            summaries.append(
                SessionSummary(
                    session_id=sid,
                    title=parsed.output.title if parsed.output else "",
                    status="paused" if paused_stage else "completed",
                    stage=paused_stage or "done",
                    cost_usd=parsed.cost_usd or 0.0,
                    duration_s=parsed.duration_seconds or 0.0,
                    doc_url=parsed.output.google_doc_url if parsed.output else "",
                    created_at=parsed.timestamp,
                    profile=parsed.profile,
                    checkpoints=self.snapshot_checkpoints(sid),
                )
            )

        summaries.sort(key=lambda s: s.created_at or "", reverse=True)
        return summaries

    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        handle = self.sessions.get(session_id)
        if not handle:
            record = latest_session_record(session_id)
            if record is None:
                snapshot = self.load_snapshot(session_id)
                if snapshot and snapshot.stage != "init":
                    return self.detail_from_snapshot(session_id, snapshot)
                return None
            parsed = HistorySessionData.model_validate(record.model_dump())
            output: CompletionOutput | None = None
            if parsed.output:
                output = CompletionOutput(
                    title=parsed.output.title,
                    google_doc_url=parsed.output.google_doc_url,
                    word_count=parsed.output.word_count,
                    review_findings_count=len(parsed.output.review_findings),
                )
            paused_stage = parsed.output.paused_after if parsed.output else ""
            return SessionDetail(
                session_id=session_id,
                title=parsed.output.title if parsed.output else "",
                status="paused" if paused_stage else "completed",
                state=SessionStateSnapshot(
                    doc_url=parsed.output.google_doc_url if parsed.output else "",
                    stage=paused_stage or "done",
                ),
                cost=CostSnapshot(
                    total_cost_usd=parsed.cost_usd or 0.0,
                    duration_s=parsed.duration_seconds or 0.0,
                ),
                created_at=parsed.timestamp,
                output=output,
                events=self.load_events(session_id),
                checkpoints=self.snapshot_checkpoints(session_id),
            )
        return SessionDetail(
            session_id=handle.session_id,
            title=handle.result.output.title if handle.result else handle.state.title,
            status=handle.status,  # type: ignore[arg-type]
            profile=handle.profile,
            state=SessionStateSnapshot(
                doc_id=handle.state.doc_id,
                doc_url=handle.state.doc_url,
                title=handle.state.title,
                stage=handle.state.stage,
                sections=[
                    SectionInfo(title=s["title"], tab_id=s["tab_id"])
                    for s in handle.state.sections
                ],
                pending_questions=list(handle.state.pending_questions),
            ),
            cost=CostSnapshot(
                total_cost_usd=handle.cost.total.cost_usd,
                total_input_tokens=handle.cost.total.input_tokens,
                total_output_tokens=handle.cost.total.output_tokens,
                duration_s=handle.cost.total.duration.total_seconds(),
                stages={
                    name: StageCostSummary(
                        cost_usd=sc.cost_usd,
                        duration_s=sc.duration.total_seconds(),
                        input_tokens=sc.input_tokens,
                        output_tokens=sc.output_tokens,
                        calls=sc.turn_count,
                    )
                    for name, sc in handle.cost.stages.items()
                },
            ),
            created_at=handle.created_at.isoformat(),
            events=handle.events[-200:],
        )

    def detail_from_snapshot(
        self, session_id: str, snapshot: PipelineSnapshot
    ) -> SessionDetail:
        sections = [
            SectionInfo(title=s.title, tab_id="")
            for s in (snapshot.plan.sections if snapshot.plan else [])
        ]
        output: CompletionOutput | None = None
        if snapshot.output:
            output = CompletionOutput(
                title=snapshot.output.title,
                google_doc_url=snapshot.output.google_doc_url,
                word_count=snapshot.output.word_count,
                review_findings_count=len(snapshot.output.review_findings),
            )
        return SessionDetail(
            session_id=session_id,
            title=snapshot.plan.title if snapshot.plan else "",
            status="interrupted",
            profile=snapshot.profile,
            state=SessionStateSnapshot(
                doc_id=snapshot.doc_id,
                doc_url=snapshot.doc_url,
                stage=snapshot.stage,
                sections=sections,
                pending_questions=list(snapshot.pending_questions),
            ),
            cost=CostSnapshot(),
            events=self.load_events(session_id),
            output=output,
            checkpoints=self.snapshot_checkpoints(session_id),
        )

    async def send_action(
        self, session_id: str, action: str, text: str | None = None
    ) -> bool:
        handle = self.sessions.get(session_id)
        if not handle or handle.status != "running":
            return False

        match action:
            case "sync":
                handle.listener.request_sync()
            case "done":
                await handle.listener.revision_queue.put(None)
            case "quit":
                handle.task.cancel()
            case "feedback":
                if text:
                    await handle.listener.feedback_queue.put([text])
            case "fetch":
                if text:
                    await self.fetch_and_queue_comments(handle, text)
            case _:
                return False
        return True

    async def fetch_and_queue_comments(
        self, handle: SessionHandle, doc_arg: str
    ) -> None:
        """Fetch comments from a Google Doc and queue them as feedback."""
        from inkwell.agent.tools.extract import is_gdoc_url, parse_gdoc_id
        from inkwell.agent.tools.google_docs import do_fetch_comments

        try:
            if is_gdoc_url(doc_arg):
                doc_id = parse_gdoc_id(doc_arg)
            else:
                doc_id = doc_arg

            already_seen = (
                handle.state.seen_comment_ids | handle.state.agent_comment_ids
            )
            comments = await do_fetch_comments(
                doc_id, exclude_ids=already_seen, include_resolved=True
            )
        except (RuntimeError, OSError, ValueError) as exc:
            await self.broadcast(
                handle.session_id,
                {
                    "type": "progress",
                    "message": f"Failed to fetch comments: {exc}",
                },
            )
            return

        if not comments:
            await self.broadcast(
                handle.session_id,
                {
                    "type": "progress",
                    "message": "No new comments found on that doc.",
                },
            )
            return

        items: list[str] = []
        for c in comments:
            tag = "Resolved GDoc Comment" if c.resolved else "GDoc Comment"
            line = f"[{tag} from {doc_id}] {c.content}"
            if c.anchor_text:
                line += f' (on: "{c.anchor_text}")'
            if c.replies:
                line += f" | Replies: {' → '.join(c.replies)}"
            items.append(line)

        await handle.listener.feedback_queue.put(items)
        await self.broadcast(
            handle.session_id,
            {
                "type": "progress",
                "message": f"Fetched {len(comments)} comment(s) from doc — queued as feedback",
            },
        )

    async def broadcast(self, session_id: str, message: dict[str, object]) -> None:
        handle = self.sessions.get(session_id)
        if not handle:
            return

        handle.events.append(message)

        stale: list[WebSocket] = []
        for ws in list(handle.clients):
            try:
                await ws.send_text(json.dumps(message, default=str))
            except Exception:
                stale.append(ws)

        for ws in stale:
            handle.clients.discard(ws)

    async def add_client(self, session_id: str, ws: WebSocket) -> bool:
        handle = self.sessions.get(session_id)
        if not handle:
            return False
        handle.clients.add(ws)
        return True

    def remove_client(self, session_id: str, ws: WebSocket) -> None:
        handle = self.sessions.get(session_id)
        if not handle:
            return
        handle.clients.discard(ws)

    async def shutdown(self) -> None:
        for handle in self.sessions.values():
            if handle.status == "running":
                handle.task.cancel()
        tasks = [h.task for h in self.sessions.values() if not h.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for handle in self.sessions.values():
            if handle.trace_holder and handle.status != "completed":
                try:
                    handle.trace_holder[0].save()
                except (OSError, RuntimeError):
                    logger.warning(
                        "Failed to save trace for %s during shutdown", handle.session_id
                    )
            self.save_events(handle)
