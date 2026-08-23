"""Agent blocks reach a watching surface before the logical turn finishes.

The runtime's display decorator is intentionally a completed replay.  Using
that replay as the web activity feed made a long single-writer turn appear to
emit only heartbeats, then appended every TextOutput, tool call, and result at
one timestamp when it ended.  These tests pin both ways Inkwell runs stages:
one-shot ``query`` calls and held cohort sessions.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from lup.actors.refs import ActorRef
from lup.runtime.contracts import EventStream, Session, Turn
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    BlockCompletedEvent,
    LiveTurnEvent,
    SessionHandle,
    SessionId,
    TurnCompletedEvent,
    TurnEvent,
    TurnHandle,
    TurnId,
    TurnIdentifiers,
    TurnRequest,
    TurnResult,
    TurnTextBlock,
    turn_request,
)
from lup.types import Usage

import inkwell.agent.client as client
from inkwell.agent.cohort import run_cohort


IDS = TurnIdentifiers(session=SessionId(value="session"), turn=TurnId(value="turn"))


class HeldTurn(Turn[None]):
    """A result held open after its first completed block is available."""

    def __init__(self, release: asyncio.Event, block: TurnTextBlock) -> None:
        self.release = release
        self.block = block

    async def result(self) -> TurnResult[None]:
        await self.release.wait()
        return TurnResult(
            output=None,
            messages=[],
            blocks=[self.block],
            usage=Usage(),
            duration=timedelta(),
            identifiers=IDS,
        )


class HeldEvents(EventStream):
    """One completed text block, followed later by terminal completion."""

    def __init__(self, release: asyncio.Event, block: TurnTextBlock) -> None:
        self.release = release
        self.block = block

    async def stream(self) -> AsyncIterator[TurnEvent]:
        yield BlockCompletedEvent(identifiers=IDS, block=self.block)
        await self.release.wait()
        yield TurnCompletedEvent(identifiers=IDS)

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.stream()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        return self.stream()


class HeldSession(Session):
    def __init__(self, release: asyncio.Event, block: TurnTextBlock) -> None:
        self.release = release
        self.block = block

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        del request
        return TurnHandle(
            turn=cast(Turn[T], HeldTurn(self.release, self.block)),
            events=HeldEvents(self.release, self.block),
        )


def held_factory(release: asyncio.Event, text: str = "drafting now") -> SessionFactory:
    block = TurnTextBlock(text=text)

    @asynccontextmanager
    async def opened(_resume: SessionId | None = None):
        yield SessionHandle(session=HeldSession(release, block))

    return SessionFactory(opened)


async def wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def test_one_shot_stage_forwards_a_block_before_its_turn_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    forwarded = asyncio.Event()
    seen: list[tuple[str, str]] = []
    factory = held_factory(release)

    async def record(block, prefix: str) -> None:
        seen.append((block.text_payload or "", prefix))
        forwarded.set()

    monkeypatch.setattr(client, "provider_factory", lambda **_kwargs: factory)
    running = asyncio.create_task(
        client.query("write", prefix="[write] ", block_callback=record)
    )

    await wait_for(forwarded)

    assert not running.done()
    assert seen == [("drafting now", "[write] ")]

    release.set()
    await running
    assert seen == [("drafting now", "[write] ")]


async def test_cohort_stage_forwards_a_block_before_its_turn_finishes(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    forwarded = asyncio.Event()
    seen: list[tuple[str, str]] = []
    factory = held_factory(release, "reviewing now")
    cohort = run_cohort(tmp_path / "cohort")
    actor = ActorRef(kind="reviewer", id="facts")

    async def record(block, prefix: str) -> None:
        seen.append((block.text_payload or "", prefix))
        forwarded.set()

    def recipe(_actor, _hooks):
        return client.observed_factory(
            factory,
            label="review",
            prefix="[review:facts] ",
            trace_logger=None,
            cost_accumulator=None,
            block_callback=record,
        )

    running = asyncio.create_task(
        cohort.round(actor, turn_request("review"), recipe, task="Review facts")
    )

    await wait_for(forwarded)

    assert not running.done()
    assert seen == [("reviewing now", "[review:facts] ")]

    release.set()
    await running
    assert seen == [("reviewing now", "[review:facts] ")]
