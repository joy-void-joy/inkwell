"""WebSocket endpoint for real-time session event streaming."""

import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from inkwell.environment.web.session_manager import SessionManager

logger = logging.getLogger(__name__)

router = APIRouter()

manager: SessionManager | None = None


def set_manager(m: SessionManager) -> None:
    global manager  # noqa: PLW0603
    manager = m


def get_manager() -> SessionManager:
    if manager is None:
        raise RuntimeError("SessionManager not initialized")
    return manager


WS_CLOSE_SESSION_NOT_FOUND = 4004


@router.websocket("/ws/_ping")
async def ws_ping(websocket: WebSocket) -> None:
    """Diagnostic endpoint — no session logic, just echo."""
    await websocket.accept()
    logger.info("PING WS: accepted")
    try:
        await websocket.send_text("pong")
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"echo: {data}")
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.info("PING WS: closed: %s", exc)


@router.websocket("/ws/claude-login/{name}")
async def claude_login_stream(websocket: WebSocket, name: str) -> None:
    """Stream a server-side login browser so a remote user can sign into claude.ai.

    The browser runs off-screen on the inkwell host (the only place that can
    capture the HttpOnly ``sessionKey`` into this profile's durable context);
    its screen is streamed to the viewer as JPEG frames and their clicks/keys
    are replayed into it. On capture, the stale pasted cookie is cleared so the
    fresh durable session becomes authoritative.
    """
    from pydantic import ValidationError

    from inkwell.agent.browser_auth import InputEvent, StreamingLoginSession
    from inkwell.devtools.setup import write_env_local

    await websocket.accept()
    session = StreamingLoginSession(name)
    try:
        await session.start()
    except RuntimeError as exc:
        await websocket.send_json({"type": "error", "detail": str(exc)})
        await websocket.close()
        return
    await websocket.send_json({"type": "ready"})

    async def forward_frames() -> None:
        try:
            while True:
                data = await session.frames.get()
                await websocket.send_json({"type": "frame", "data": data})
        except (WebSocketDisconnect, RuntimeError):
            return

    async def forward_input() -> None:
        while True:
            try:
                message = await websocket.receive_json()
            except (WebSocketDisconnect, ValueError):
                return
            try:
                event = InputEvent.model_validate(message)
            except ValidationError:
                continue
            await session.apply_input(event)

    async def await_capture() -> None:
        await session.wait_for_session()
        write_env_local({"CLAUDE_COOKIE": ""}, profile=name)
        with suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json({"type": "done"})

    tasks = [
        asyncio.create_task(forward_frames()),
        asyncio.create_task(forward_input()),
        asyncio.create_task(await_capture()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await session.aclose()
        with suppress(RuntimeError):
            await websocket.close()


@router.websocket("/ws/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    mgr = get_manager()
    await websocket.accept()
    logger.info(
        "WS %s: accepted, handle_exists=%s", session_id, session_id in mgr.sessions
    )

    handle = mgr.sessions.get(session_id)

    if handle is None:
        saved_events = mgr.load_events(session_id)
        logger.info(
            "WS %s: no handle, replaying %d saved events", session_id, len(saved_events)
        )
        for event in saved_events:
            try:
                await websocket.send_text(json.dumps(event, default=str))
            except (RuntimeError, OSError, ConnectionError):
                logger.info("WS %s: send failed during replay", session_id)
                return
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "session_ended",
                        "status": "completed",
                        "timestamp": datetime.now().isoformat(),
                    }
                )
            )
            await websocket.close(
                code=WS_CLOSE_SESSION_NOT_FOUND, reason="Session not found"
            )
        except (RuntimeError, OSError, ConnectionError):
            logger.info("WS %s: send/close failed for session_ended", session_id)
        return

    logger.info(
        "WS %s: handle found, status=%s, events=%d",
        session_id,
        handle.status,
        len(handle.events),
    )
    handle.clients.add(websocket)

    replayed = len(handle.events)
    for event in handle.events[:replayed]:
        try:
            await websocket.send_text(json.dumps(event, default=str))
        except (RuntimeError, OSError, ConnectionError):
            logger.info("WS %s: send failed during handle replay", session_id)
            mgr.remove_client(session_id, websocket)
            return

    if handle.status != "running":
        logger.info(
            "WS %s: session not running (%s), sending ended + close",
            session_id,
            handle.status,
        )
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "session_ended",
                        "status": handle.status,
                        "timestamp": datetime.now().isoformat(),
                    }
                )
            )
            await websocket.close(code=1000)
        except (RuntimeError, OSError, ConnectionError):
            pass
        finally:
            mgr.remove_client(session_id, websocket)
        return

    if handle.listener.awaiting_revision:
        state_snapshot = handle.listener.serialize_state(handle.state)
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "collect_revision",
                        "state": state_snapshot,
                        "timestamp": datetime.now().isoformat(),
                    },
                    default=str,
                )
            )
        except (RuntimeError, OSError, ConnectionError):
            mgr.remove_client(session_id, websocket)
            return

    logger.info("WS %s: entering receive loop", session_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            match msg_type:
                case "feedback":
                    items = msg.get("items", [])
                    if items:
                        await handle.listener.feedback_queue.put(items)
                case "revision":
                    text = msg.get("text")
                    await handle.listener.revision_queue.put(text)
                case "action":
                    action = msg.get("action", "")
                    await mgr.send_action(session_id, action, msg.get("text"))

    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.info("WS %s: receive loop ended: %s", session_id, exc)
    finally:
        mgr.remove_client(session_id, websocket)
