"""A credential the runtime could not refresh holds a turn instead of ending it.

lup already starts a turn the provider failed again, once and at once, which is
the whole repair for a turn that simply broke. An unrefreshable credential is
the one provider failure where *at once* is the wrong moment: the refresh that
failed is usually one this process is racing.

Three reviewers opened fifty milliseconds apart, all met one expired token, all
tried to refresh it, and all failed inside 1.6 seconds — two attempts each, the
recovery retry included. The stage three minutes behind them authenticated
without trouble, so the run rewrote against a review that never ran. These pin
that a pause is composed at the one site every model session is built through.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast

import pytest
from pydantic import BaseModel

from lup.runtime.contracts import Session, Turn
from lup.runtime.errors import ProviderTurnError, TurnFailure
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
from lup.types import Usage

import inkwell.agent.client as client


IDS = TurnIdentifiers(session=SessionId(value="session"), turn=TurnId(value="turn"))

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"
"""Verbatim what the runtime said to all three reviewers of session 2b1b2e19."""

IMPATIENT = client.CredentialRetryConfig(first_wait_seconds=0.001, backoff=1)
"""The same policy with the waiting taken out, so a test runs in real time."""


class RefusedThenServed(Turn[None]):
    """Refused for its credential on the first results, answered on the next."""

    def __init__(self, refusals: list[str]) -> None:
        self.refusals = refusals

    async def result(self) -> TurnResult[None]:
        if self.refusals:
            raise ProviderTurnError(
                TurnFailure(
                    message=self.refusals.pop(),
                    blocks=[],
                    identifiers=IDS,
                    environmental=True,
                )
            )
        return TurnResult(
            output=None,
            messages=[],
            blocks=[TurnTextBlock(text="reviewed after the wait")],
            usage=Usage(),
            duration=timedelta(),
            identifiers=IDS,
        )


class RefusingSession(Session):
    """Every turn started here shares one budget of refusals."""

    def __init__(self, refusals: list[str]) -> None:
        self.refusals = refusals
        self.starts = 0

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        del request
        self.starts += 1
        return TurnHandle(turn=cast(Turn[T], RefusedThenServed(self.refusals)))


def refusing_factory(session: RefusingSession) -> SessionFactory:
    @asynccontextmanager
    async def opened(
        _resume: SessionId | None = None,
    ) -> AsyncIterator[SessionHandle]:
        yield SessionHandle(session=session)

    return SessionFactory(opened)


def composed(
    monkeypatch: pytest.MonkeyPatch, session: RefusingSession
) -> client.AgentSurface:
    """The surface every model session in the application is built through."""
    monkeypatch.setattr(client, "CREDENTIALS", IMPATIENT)
    monkeypatch.setattr(
        client, "provider_factory", lambda **_kwargs: refusing_factory(session)
    )
    return client.AgentSurface()


async def test_a_lost_credential_waits_and_runs_the_same_request_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two refusals, because lup's own recovery retry absorbs the first at
    once. It is the second that reaches this decorator, and the second is what
    the reviewers met: they each failed twice inside 1.6 seconds."""
    session = RefusingSession([EXPIRED, EXPIRED])
    surface = composed(monkeypatch, session)

    with surface.activate():
        result = await client.query("review the draft", prefix="[review] ")

    assert client.result_text(result) == "reviewed after the wait"
    assert session.starts == 3


async def test_it_gives_up_once_the_attempts_are_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential genuinely revoked wants somebody to log in again, and
    sleeping on it only delays their finding out. The failure still reaches the
    stage, which now stops the run rather than reporting a short wave.

    The two decorators compose multiplicatively — lup starts a broken turn
    again once inside each of these attempts — so the ceiling is the product,
    and it is worth knowing that the bound is six provider turns rather than
    three.
    """
    session = RefusingSession([EXPIRED] * 20)
    surface = composed(monkeypatch, session)

    with surface.activate():
        with pytest.raises(ProviderTurnError):
            await client.query("review the draft", prefix="[review] ")

    assert session.starts == IMPATIENT.attempts * (client.RECOVERY.retries + 1)


async def test_a_provider_failure_that_is_not_a_credential_is_not_slept_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lup already starts a broken turn again at once, which is the whole
    repair for it. Holding every provider failure would sit a stage down for
    seconds over a turn that would have answered immediately."""
    session = RefusingSession(["the model went away"] * 10)
    surface = composed(monkeypatch, session)

    with surface.activate():
        with pytest.raises(ProviderTurnError):
            await client.query("review the draft", prefix="[review] ")

    # Once for the turn, once for lup's own immediate recovery retry, and no
    # further attempt bought by this decorator.
    assert session.starts == 2


def test_only_a_failure_naming_both_marks_counts_as_a_lost_credential() -> None:
    """Matched on the message because no typed error carries the distinction,
    so the match has to be narrow enough not to catch a turn that merely
    mentions authenticating."""
    failure = TurnFailure(message=EXPIRED, blocks=[], identifiers=IDS)

    assert client.lost_credential(ProviderTurnError(failure))
    assert not client.lost_credential(
        ProviderTurnError(
            TurnFailure(
                message="the tool failed to authenticate against the corpus",
                blocks=[],
                identifiers=IDS,
            )
        )
    )


def test_each_attempt_waits_longer_than_the_one_before() -> None:
    """Agents that raced each other into one failure come back at different
    moments rather than re-racing on a single schedule."""
    config = client.CredentialRetryConfig()

    waits = [config.wait_after(attempt) for attempt in range(3)]

    assert waits == sorted(waits)
    assert len(set(waits)) == len(waits)
