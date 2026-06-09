"""WebListener — PipelineListener that broadcasts events over WebSocket."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from inkwell.agent.models import WritingOutput
from inkwell.agent.pipeline import PipelineListener
from inkwell.agent.session import WritingSessionState
from inkwell.agent.tools.google_docs import do_insert_comment
from inkwell.environment.web.models import (
    CostSnapshot,
    SectionInfo,
    SessionStateSnapshot,
    StageCostSummary,
)

if TYPE_CHECKING:
    from inkwell.environment.web.session_manager import SessionManager

logger = logging.getLogger(__name__)


class WebListener(PipelineListener):
    """Pipeline listener that broadcasts events to WebSocket clients."""

    def __init__(self, session_id: str, manager: SessionManager) -> None:
        super().__init__()
        self.session_id = session_id
        self.manager = manager
        self.feedback_queue: asyncio.Queue[list[str]] = asyncio.Queue()
        self.revision_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.awaiting_revision = False

    async def broadcast(self, message: dict[str, object]) -> None:
        message.setdefault("timestamp", datetime.now().isoformat())
        await self.manager.broadcast(self.session_id, message)

    async def broadcast_state(self) -> None:
        handle = self.manager.sessions.get(self.session_id)
        if not handle:
            return
        await self.broadcast({"type": "state_update", "state": self.serialize_state(handle.state)})

    async def broadcast_cost(self) -> None:
        handle = self.manager.sessions.get(self.session_id)
        if not handle:
            return
        acc = handle.cost
        snapshot = CostSnapshot(
            total_cost_usd=acc.total_cost_usd,
            total_input_tokens=acc.total_input_tokens,
            total_output_tokens=acc.total_output_tokens,
            duration_s=acc.duration_seconds,
            stages={
                name: StageCostSummary(
                    cost_usd=sc.cost_usd,
                    duration_s=sc.duration_seconds,
                    input_tokens=sc.input_tokens,
                    output_tokens=sc.output_tokens,
                    calls=sc.call_count,
                )
                for name, sc in acc.stages.items()
            },
        )
        await self.broadcast({"type": "cost_update", "cost": snapshot.model_dump()})

    def serialize_state(self, state: WritingSessionState) -> dict[str, object]:
        return SessionStateSnapshot(
            doc_id=state.doc_id,
            doc_url=state.doc_url,
            title=state.title,
            stage=state.stage,
            sections=[SectionInfo(title=s["title"], tab_id=s["tab_id"]) for s in state.sections],
            pending_questions=list(state.pending_questions),
        ).model_dump()

    async def on_block(self, block_type: str, content: str, prefix: str) -> None:
        await self.broadcast({
            "type": "block",
            "block_type": block_type,
            "content": content[:2000],
            "prefix": prefix,
        })

    async def on_stage(self, stage: str, description: str) -> None:
        await self.broadcast({"type": "stage", "stage": stage, "description": description})
        await self.broadcast_state()
        await self.broadcast_cost()

    async def on_progress(self, message: str) -> None:
        await self.broadcast({"type": "progress", "message": message})

    async def on_complete(self, output: WritingOutput) -> None:
        await self.broadcast({
            "type": "complete",
            "output": {
                "title": output.title,
                "word_count": output.word_count,
                "summary": output.summary,
                "google_doc_url": output.google_doc_url,
                "review_findings_count": len(output.review_findings),
            },
        })
        await self.broadcast_cost()

    async def on_message(self, source: str, message: str) -> None:
        await self.broadcast({"type": "message", "source": source, "message": message})

    async def collect_feedback(self, state: WritingSessionState) -> list[str]:
        feedback = await super().collect_feedback(state)

        await self.broadcast({"type": "state_update", "state": self.serialize_state(state)})

        web_items: list[str] = []
        while not self.feedback_queue.empty():
            try:
                batch = self.feedback_queue.get_nowait()
                web_items.extend(batch)
            except asyncio.QueueEmpty:
                break

        for msg in web_items:
            feedback.append(f"[Terminal] Author direction: {msg}")
            if state.doc_id:
                try:
                    await do_insert_comment(
                        state.doc_id,
                        f"[TERMINAL] {msg}",
                        session_state=state,
                    )
                except (RuntimeError, OSError):
                    logger.warning("Failed to mirror web input to GDoc")

        if feedback:
            await self.broadcast({
                "type": "progress",
                "message": f"Incorporating {len(feedback)} feedback item(s)",
            })

        return feedback

    async def collect_revision(self, state: WritingSessionState) -> str | None:
        revisions: list[str] = []
        while not self.revision_queue.empty():
            try:
                item = self.revision_queue.get_nowait()
                if item is None:
                    return None
                revisions.append(item)
            except asyncio.QueueEmpty:
                break
        if revisions:
            return " | ".join(revisions)

        self.awaiting_revision = True
        try:
            await self.broadcast({
                "type": "collect_revision",
                "state": self.serialize_state(state),
            })
            return await asyncio.wait_for(self.revision_queue.get(), timeout=300)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            return None
        finally:
            self.awaiting_revision = False
