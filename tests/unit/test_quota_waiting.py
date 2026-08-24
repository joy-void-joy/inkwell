"""An exhausted account allowance holds a run open instead of ending it.

The provider saying "not yet" and the provider saying "no" arrive as the same
exception at the same place, and a stage that treats them alike throws away a
plan, its research, and a part-written draft over a window that rolls in
minutes. lup ships the waiter; these tests pin that Inkwell composes it at the
one site every model session in the application is built through.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import BaseModel

from lup.runtime.contracts import Session, Turn
from lup.runtime.errors import QuotaExceededError, TurnFailure
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnHandle,
    TurnId,
    TurnIdentifiers,
    TurnRequest,
    TurnResult,
    TurnTextBlock,
)
from lup.runtime.quota import QuotaWaitConfig, QuotaWaitEvent
from lup.types import Usage

import inkwell.agent.client as client


IDS = TurnIdentifiers(session=SessionId(value="session"), turn=TurnId(value="turn"))

IMPATIENT = QuotaWaitConfig(
    minimum_wait_seconds=0, unknown_reset_wait_seconds=0.01, reset_grace_seconds=0
)
"""The same policy with the sleeping taken out, so a test runs in real time."""


class ExhaustedThenServed(Turn[None]):
    """Refused for the allowance on its first result, answered on the next."""

    def __init__(self, refusals: list[None], reset_at: datetime | None) -> None:
        self.refusals = refusals
        self.reset_at = reset_at

    async def result(self) -> TurnResult[None]:
        if self.refusals:
            self.refusals.pop()
            raise QuotaExceededError(
                TurnFailure(
                    message="Claude account allowance exhausted",
                    blocks=[],
                    identifiers=IDS,
                    environmental=True,
                ),
                reset_at=self.reset_at,
                quota_type="five_hour",
            )
        return TurnResult(
            output=None,
            messages=[],
            blocks=[TurnTextBlock(text="drafted after the wait")],
            usage=Usage(),
            duration=timedelta(),
            identifiers=IDS,
        )


class ExhaustedSession(Session):
    """Every turn started here shares one budget of refusals."""

    def __init__(self, refusals: list[None], reset_at: datetime | None) -> None:
        self.refusals = refusals
        self.reset_at = reset_at
        self.starts = 0

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        del request
        self.starts += 1
        return TurnHandle(
            turn=cast(Turn[T], ExhaustedThenServed(self.refusals, self.reset_at)),
        )


def exhausted_factory(
    session: ExhaustedSession,
) -> SessionFactory:
    @asynccontextmanager
    async def opened(
        _resume: SessionId | None = None,
    ) -> AsyncIterator[SessionHandle]:
        yield SessionHandle(session=session)

    return SessionFactory(opened)


async def test_an_exhausted_allowance_waits_and_runs_the_same_request_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ExhaustedSession([None], reset_at=None)
    waits: list[QuotaWaitEvent] = []

    async def record(event: QuotaWaitEvent) -> None:
        waits.append(event)

    monkeypatch.setattr(client, "QUOTA_WAIT", IMPATIENT)
    monkeypatch.setattr(
        client, "provider_factory", lambda **_kwargs: exhausted_factory(session)
    )

    surface = client.AgentSurface(quota_callback=record)
    with surface.activate():
        result = await client.query("write the part", prefix="[write] ")

    assert client.result_text(result) == "drafted after the wait"
    assert session.starts == 2
    assert [one.phase for one in waits] == ["sleep", "wake"]
    assert waits[0].quota_type == "five_hour"


def test_a_watcher_is_told_the_hour_the_run_resumes() -> None:
    reset_at = datetime.now(UTC) + timedelta(hours=1)

    said = client.quota_wait_message(
        QuotaWaitEvent(
            phase="sleep", reset_at=reset_at, wait_seconds=3600, quota_type="five_hour"
        )
    )

    assert "waiting" in said
    assert reset_at.astimezone().strftime("%H:%M") in said


def test_a_reset_the_provider_never_stated_is_told_as_a_duration() -> None:
    said = client.quota_wait_message(
        QuotaWaitEvent(phase="sleep", reset_at=None, wait_seconds=300)
    )

    assert "in 5 min" in said


def test_waking_says_the_run_continues_rather_than_restarts() -> None:
    said = client.quota_wait_message(QuotaWaitEvent(phase="wake", wait_seconds=0))

    assert said == "Allowance reset — resuming where the run left off"
