"""A reader that forgot to submit is asked again rather than written off.

A turn that did its work and ended without calling its submission tool has the
answer in its own context — the runtime marks the failure correctable for
exactly that reason. Unwired, a fact-check reader that read the page, found
what it was sent for, and then stopped short of submitting was recorded as
failed, and what it established stayed in a transcript nobody reads.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast

import pytest
from pydantic import BaseModel, Field

from lup.runtime.contracts import Session, Turn
from lup.runtime.errors import StructuredOutputError, TurnFailure
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnHandle,
    TurnId,
    TurnIdentifiers,
    TurnRequest,
    TurnResult,
)
from lup.types import Usage

import inkwell.agent.client as client


IDS = TurnIdentifiers(session=SessionId(value="session"), turn=TurnId(value="turn"))


class Verdict(BaseModel):
    """What a checking reader is asked to hand back."""

    finding: str = Field(description="What the reader established")


class ForgetsThenSubmits(Turn[Verdict]):
    """Completes without a submission first, and submits when asked again."""

    def __init__(self, lapses: list[None]) -> None:
        self.lapses = lapses

    async def result(self) -> TurnResult[Verdict]:
        if self.lapses:
            self.lapses.pop()
            raise StructuredOutputError(
                TurnFailure(
                    message="turn completed without a valid submit_output call",
                    blocks=[],
                    identifiers=IDS,
                    correctable=True,
                )
            )
        return TurnResult(
            output=Verdict(finding="GTG-1002 does not appear on the page"),
            messages=[],
            blocks=[],
            usage=Usage(),
            duration=timedelta(),
            identifiers=IDS,
        )


class ForgetfulSession(Session):
    """Every turn started here draws on one shared budget of lapses."""

    def __init__(self, lapses: list[None]) -> None:
        self.lapses = lapses
        self.asked: list[str] = []

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        self.asked.append(request.input.text)
        return TurnHandle(turn=cast(Turn[T], ForgetsThenSubmits(self.lapses)))


def forgetful_factory(session: ForgetfulSession) -> SessionFactory:
    @asynccontextmanager
    async def opened(
        _resume: SessionId | None = None,
    ) -> AsyncIterator[SessionHandle]:
        yield SessionHandle(session=session)

    return SessionFactory(opened)


async def test_a_turn_that_never_submitted_is_asked_for_its_answer_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ForgetfulSession([None])
    monkeypatch.setattr(
        client, "provider_factory", lambda **_kwargs: forgetful_factory(session)
    )

    verdict = await client.query(
        "Check whether the page names GTG-1002", output_type=Verdict
    )

    assert verdict is not None
    assert verdict.finding == "GTG-1002 does not appear on the page"
    assert len(session.asked) == 2
    assert "Correction required" in session.asked[1]


async def test_a_reader_that_keeps_forgetting_still_fails_in_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forever = [None] * 20
    session = ForgetfulSession(forever)
    monkeypatch.setattr(
        client, "provider_factory", lambda **_kwargs: forgetful_factory(session)
    )

    with pytest.raises(StructuredOutputError):
        await client.query("Check the page", output_type=Verdict)

    assert len(session.asked) == client.CORRECTION.cycles + 1
