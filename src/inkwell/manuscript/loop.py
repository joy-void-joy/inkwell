"""The pass that takes a work from wherever it stands to rest.

The build loop. It asks what is out of date, runs those parts, records what
each one read and moved, and asks again — until a pass finds nothing, which is
what "done" means for a book somebody keeps changing.

**A pass is bounded and a loop is not.** One pass runs what is dirty *now*.
Running a part can dirty others, so the loop is passes until a pass adds
nothing — and because propagation keys on change facts rather than on nodes,
that terminates: a rewrite that moves nothing shared makes the next pass
smaller, never larger. The pass cap is a backstop against a work whose parts
genuinely keep changing each other, not the mechanism that ends the loop.

**The loop is the single writer of state.** Every part's standing, stamp, and
change fact is written here, and nowhere else — which is what lets parts run
concurrently with no lock: they write prose, into spans that cannot overlap,
and hand back what they did. The state is published after each part rather
than at the end of a pass, so an interrupted pass loses one part's work rather
than a hundred parts' worth.

**Parked is not failed.** A run that asks a question ends the part's turn
without ending its life: the question goes to the mailbox, the part parks, and
nothing retries it until somebody answers. A failed part records what it said
and stays failed, because a part that quietly returned to idle would be picked
up next pass and fail the same way for the same reason, forever.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator

from pydantic import BaseModel, ConfigDict, Field

from inkwell.manuscript.graph import WorkSweep, readings, sweep
from inkwell.manuscript.mailbox import (
    PartMailbox,
    PartQuestion,
    escalated_to,
    question_id,
)
from inkwell.manuscript.reconcile import reconcile
from inkwell.manuscript.runner import PartOutcome, TurnEnding, run_part
from inkwell.manuscript.state import NodeVerdict, WorkState
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript
from inkwell.manuscript.vocabulary import Abbreviation

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
"""How many parts run at once unless a caller says otherwise.

Low on purpose. Each part is a whole pipeline run with its own model spend, so
this bounds cost as much as it bounds load, and a book-sized fan-out is a
decision somebody should make deliberately rather than inherit.
"""

DEFAULT_PASSES = 5
"""How many passes a loop takes before it stops and says what is left.

A backstop, not the mechanism: propagation settles on its own, and a loop that
reaches this has found a work whose parts really do keep moving each other,
which is worth reporting rather than grinding through.
"""

HELD_STANDINGS = ("running", "parked")
"""Standings a pass leaves alone: work in flight, and work awaiting an answer."""


class PartResult(BaseModel):
    """What became of one part in one pass, as the report reads it."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part")
    outcome: TurnEnding = Field(description="How its turn ended")
    detail: str = Field(default="", description="What it said, where it said anything")


class Recorded(BaseModel):
    """One part's result and the state that recording it produced.

    Both together because recording a part moves the state, and the next part
    is recorded against the moved one — a result on its own would leave the
    caller reconstructing what the state became.
    """

    model_config = ConfigDict(frozen=True)

    state: WorkState = Field(description="The work's state after this part")
    result: PartResult = Field(description="What became of the part")


class PassReport(BaseModel):
    """What one pass over a work did, and what it left."""

    work: str = Field(description="The work")
    scheduled: tuple[str, ...] = Field(
        default=(), description="The parts this pass picked up"
    )
    results: tuple[PartResult, ...] = Field(
        default=(), description="What became of each of them"
    )
    conflicts: tuple[str, ...] = Field(
        default=(),
        description="What the reconciler found the wave's rewrites disagreed on",
    )
    remaining: int = Field(default=0, description="Parts still outstanding after it")

    def settled(self) -> bool:
        """Whether the work came to rest in this pass."""
        return self.remaining == 0

    def counted(self, outcome: TurnEnding) -> int:
        """How many parts this pass left in one condition."""
        return sum(1 for held in self.results if held.outcome == outcome)

    def render(self) -> str:
        """This pass as a line somebody watching the loop reads."""
        return (
            f"{len(self.scheduled)} run — {self.counted('rewritten')} rewritten, "
            f"{self.counted('parked')} parked, {self.counted('failed')} failed; "
            f"{self.remaining} outstanding"
        )


def schedulable(found: WorkSweep, state: WorkState) -> tuple[NodeVerdict, ...]:
    """The parts a pass may pick up, which is not everything that is dirty.

    A part another run is holding is left alone, and so is one parked on a
    question nobody has answered — picking either up would either duplicate
    work or re-ask what is already asked and get no further.
    """
    return tuple(
        verdict
        for verdict in found.dirty()
        if state.standing(verdict.key) not in HELD_STANDINGS
    )


def parked_by(
    mailbox: PartMailbox, manuscript: Manuscript, outcome: PartOutcome, work: str
) -> str:
    """Record what a part asked, and say why it is parking.

    Every question is filed before the part parks, so an answer can arrive
    against any of them; the reason names the first, because a status line has
    room for one and the mailbox holds the rest.
    """
    for prompt in outcome.questions:
        mailbox.ask(
            PartQuestion(
                id=question_id(outcome.key, prompt),
                work=work,
                asker=outcome.key,
                addressed_to=escalated_to(manuscript, outcome.key),
                prompt=prompt,
                session=outcome.session,
            )
        )
    return outcome.questions[0] if outcome.questions else "waiting on an answer"


def recording(
    state: WorkState,
    verdict: NodeVerdict,
    outcome: PartOutcome | BaseException,
    mailbox: PartMailbox,
    manuscript: Manuscript,
    work: str,
) -> Recorded:
    """What one part's turn did to the work, in the four ways it can end.

    A raised turn is narrowed first because an exception is not an outcome at
    all — nothing came back to ask. The three real endings are asked of the
    outcome rather than worked out from its fields here, so a fourth way to
    end is one literal and one arm instead of a condition this function got
    wrong.
    """
    if isinstance(outcome, BaseException):
        logger.exception("Running %s raised", verdict.key, exc_info=outcome)
        return Recorded(
            state=state.declared(verdict.key, "failed", str(outcome)),
            result=PartResult(key=verdict.key, outcome="failed", detail=str(outcome)),
        )
    match outcome.ended():
        case "parked":
            reason = parked_by(mailbox, manuscript, outcome, work)
            return Recorded(
                state=state.declared(verdict.key, "parked", reason),
                result=PartResult(key=verdict.key, outcome="parked", detail=reason),
            )
        case "failed":
            return Recorded(
                state=state.declared(verdict.key, "failed", outcome.failure),
                result=PartResult(
                    key=verdict.key, outcome="failed", detail=outcome.failure
                ),
            )
        case "rewritten":
            moved = state.changed(outcome.key, outcome.changes)
            return Recorded(
                state=moved.built(
                    outcome.key,
                    source=outcome.text,
                    consumed=outcome.consumed,
                    run=outcome.session,
                ),
                result=PartResult(key=verdict.key, outcome="rewritten"),
            )


async def run_pass(
    store: ManuscriptStore,
    work: str,
    manuscript: Manuscript,
    *,
    vocabulary: tuple[Abbreviation, ...] = (),
    limit: int = 0,
    concurrency: int = DEFAULT_CONCURRENCY,
    reconciling: bool = True,
) -> PassReport:
    """Run every part a pass may pick up, and record what each one did."""
    mailbox = PartMailbox(root=store.work_dir(work))
    held = readings(manuscript, vocabulary)
    opening = store.load_state(work)
    picked = schedulable(sweep(opening, held), opening)
    if limit:
        picked = picked[:limit]

    by_key = {reading.node.key: reading for reading in held}
    gate = asyncio.Semaphore(concurrency)

    async def attempt(verdict: NodeVerdict) -> PartOutcome:
        """One part's turn, with the concurrency cap held for its duration."""
        async with gate:
            return await run_part(
                store,
                work,
                manuscript,
                by_key[verdict.key].node,
                session_id=uuid.uuid4().hex[:16],
                reasons=verdict.reasons,
                vocabulary=vocabulary,
            )

    leased = opening
    for verdict in picked:
        leased = leased.declared(verdict.key, "running", "picked up by a pass")
    store.publish_state(work, leased)

    outcomes = await asyncio.gather(
        *(attempt(verdict) for verdict in picked), return_exceptions=True
    )

    def steps() -> Iterator[Recorded]:
        """Each part recorded against the state the one before it produced."""
        state = leased
        for verdict, outcome in zip(picked, outcomes):
            step = recording(state, verdict, outcome, mailbox, manuscript, work)
            state = step.state
            store.publish_state(work, state)
            yield step

    taken = tuple(steps())
    settled = taken[-1].state if taken else leased

    rewritten = [
        step.result.key for step in taken if step.result.outcome == "rewritten"
    ]
    after = readings(manuscript, vocabulary)
    conflicts = ()
    if reconciling and len(rewritten) > 1:
        found = await reconcile(
            manuscript, [held for held in after if held.node.key in rewritten]
        )
        conflicts = tuple(found.conflicts)
        for conflict in conflicts:
            settled = settled.changed(conflict.between, conflict.changes())
        store.publish_state(work, settled)

    return PassReport(
        work=work,
        scheduled=tuple(verdict.key for verdict in picked),
        results=tuple(step.result for step in taken),
        conflicts=tuple(conflict.detail for conflict in conflicts),
        remaining=len(schedulable(sweep(settled, after), settled)),
    )


async def run_loop(
    store: ManuscriptStore,
    work: str,
    manuscript: Manuscript,
    *,
    vocabulary: tuple[Abbreviation, ...] = (),
    passes: int = DEFAULT_PASSES,
    limit: int = 0,
    concurrency: int = DEFAULT_CONCURRENCY,
    reconciling: bool = True,
) -> tuple[PassReport, ...]:
    """Pass over a work until it settles, or until the cap says to stop.

    Stops early on a pass that scheduled nothing, which is either a settled
    work or one where everything outstanding is parked on a question — and in
    both cases running again would do exactly as much.
    """

    async def taken() -> AsyncIterator[PassReport]:
        """Each pass, stopping as soon as another would find nothing to do."""
        for number in range(1, passes + 1):
            report = await run_pass(
                store,
                work,
                manuscript,
                vocabulary=vocabulary,
                limit=limit,
                concurrency=concurrency,
                reconciling=reconciling,
            )
            logger.info("Pass %d of %s: %s", number, work, report.render())
            yield report
            if not report.scheduled or report.settled():
                return

    return tuple([report async for report in taken()])
