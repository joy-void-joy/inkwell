"""WebSocket endpoint for real-time session event streaming."""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from inkwell.environment.web.models import (
    ClientMessage,
    CollectRevisionEvent,
    SessionEndedEvent,
)
from inkwell.environment.web.session_manager import ManagerHolder, SessionManager

logger = logging.getLogger(__name__)

router = APIRouter()

holder = ManagerHolder()
"""Filled in at app startup, so a route reaches the manager without a global."""


def set_manager(m: SessionManager) -> None:
    holder.manager = m


def get_manager() -> SessionManager:
    return holder.require()


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


@router.websocket("/ws/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    mgr = get_manager()
    await websocket.accept()
    logger.info(
        "WS %s: accepted, handle_exists=%s", session_id, session_id in mgr.sessions
    )

    handle = mgr.handle_for(session_id)

    if handle is None:
        saved_events = mgr.load_events(session_id)
        logger.info(
            "WS %s: no handle, replaying %d saved events", session_id, len(saved_events)
        )
        for event in saved_events:
            try:
                await websocket.send_text(event.model_dump_json())
            except (RuntimeError, OSError, ConnectionError):
                logger.info("WS %s: send failed during replay", session_id)
                return
        try:
            await websocket.send_text(
                SessionEndedEvent(status="completed").model_dump_json()
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
    handle.attach_client(websocket)

    replayed = len(handle.events)
    for event in handle.events[:replayed]:
        try:
            await websocket.send_text(event.model_dump_json())
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
                SessionEndedEvent(status=handle.status).model_dump_json()
            )
            await websocket.close(code=1000)
        except (RuntimeError, OSError, ConnectionError):
            pass
        finally:
            mgr.remove_client(session_id, websocket)
        return

    if handle.listener.awaiting_revision:
        try:
            await websocket.send_text(
                CollectRevisionEvent(
                    state=handle.listener.serialize_state(handle.state)
                ).model_dump_json()
            )
        except (RuntimeError, OSError, ConnectionError):
            mgr.remove_client(session_id, websocket)
            return

    logger.info("WS %s: entering receive loop", session_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = ClientMessage.model_validate_json(raw)
            except ValidationError:
                continue

            match message.type:
                case "feedback":
                    if message.items:
                        await handle.listener.feedback_queue.put(message.items)
                case "revision":
                    await handle.listener.revision_queue.put(message.text)
                case "action":
                    await mgr.send_action(session_id, message.action, message.text)

    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.info("WS %s: receive loop ended: %s", session_id, exc)
    finally:
        mgr.remove_client(session_id, websocket)
