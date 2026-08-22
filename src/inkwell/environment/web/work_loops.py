"""The build loop over a work, held as a background task the browser can watch.

A session is one run and the browser drives it directly. A loop over a work is
not one run — it is however many runs the work turns out to need, over however
long that takes — so what the browser needs from it is different: start it, be
told what each pass did while it is still going, and be able to stop it.

**One loop per work, refused rather than queued.** The loop is the single writer
of a work's state, and two of them over one book would each lease parts the
other had already leased. A second request is therefore an error and not a wait:
whoever asked has a loop already, and telling them so is more useful than
starting something that would corrupt the first.

**Stopping is cancelling, and it is safe where it lands.** State is published
after each part rather than at the end of a pass, so a cancelled loop loses the
turn in flight and nothing else. The parts it had leased stay ``running`` with
the run that held them named, which is what `manuscript clear` is for — a lease
nobody is honouring is exactly the thing a person should be asked about rather
than silently reclaimed.
"""

import asyncio
import logging
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from lup.channels.models import utc_now

from inkwell.environment.web.models import SessionStatus
from inkwell.environment.web.session_manager import SessionManager
from inkwell.manuscript.loop import PassReport, run_loop
from inkwell.manuscript.runner import PartOutcome, PartRunObservers, PartRunWatch
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript
from inkwell.manuscript.vocabulary import Abbreviation

logger = logging.getLogger(__name__)


def ended_as(outcome: PartOutcome) -> SessionStatus:
    """How a finished part run reads as a session.

    A part that parked is not finished and not broken: it asked the author
    something and is waiting, which is the same standing as a run that stopped
    at a checkpoint, and the surface already knows how to offer that one a way
    back in.
    """
    match outcome.ended():
        case "rewritten":
            return "completed"
        case "parked":
            return "paused"
        case "failed":
            return "failed"


class LoopAlreadyRunning(Exception):
    """Raised where a work already has a loop over it."""


class WatchedByTheBrowser(PartRunWatch):
    """Registers each part run as a session, so the browser can watch it.

    A part run was always a session — a trace, a stage, a document, a spend —
    but one the web layer had never been told about, so it was read back off
    its own unfinished record and reported as interrupted for as long as it
    ran. What was missing was not a screen but the introduction: told when a
    run opens, this hands it somewhere to publish and lets every surface that
    already renders a session render this one.
    """

    def __init__(self, sessions: SessionManager) -> None:
        self.sessions = sessions

    def opening(self, key: str, session: str) -> PartRunObservers:
        """Adopt this part's run and hand it the manager's instruments.

        The task cancelled to stop it is this one — ``opening`` is called from
        inside the part's own turn, so the running task *is* that turn. Taken
        here rather than passed in, because a run nobody can stop is one an
        author has to take the whole loop down to be rid of.
        """
        running = asyncio.current_task()
        handle = self.sessions.adopt(
            session, stopping=running.cancel if running else None
        )
        logger.info("Part %s is running as session %s", key, session)
        return PartRunObservers(
            listener=handle.listener,
            state=handle.state,
            cost=handle.cost,
            trace=handle.trace,
        )

    def closed(self, key: str, session: str, outcome: PartOutcome) -> None:
        """Record how it ended, in the words the outcome already answers in."""
        self.sessions.retire(session, ended_as(outcome))


class WorkLoopStatus(BaseModel):
    """Where one work's loop stands, as something watching it reads.

    Carries every pass so far rather than only the last: a watcher that
    connected late, or reloaded the page, would otherwise have no account of
    what the loop had already done.
    """

    work: str = Field(description="The work being taken to rest")
    running: bool = Field(description="Whether a loop is in flight right now")
    started_at: datetime | None = Field(
        default=None, description="When the loop began, where one has"
    )
    finished_at: datetime | None = Field(
        default=None, description="When it stopped, where it has"
    )
    passes: tuple[PassReport, ...] = Field(
        default=(), description="What each completed pass did, oldest first"
    )
    stopped: bool = Field(
        default=False, description="Whether it ended because somebody stopped it"
    )
    failure: str = Field(
        default="", description="What went wrong, where the loop raised"
    )

    def settled(self) -> bool:
        """Whether the loop ran out of work rather than out of passes."""
        return bool(self.passes) and self.passes[-1].settled()


class WorkLoop(BaseModel):
    """One loop in flight, with what it has reported so far."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    work: str = Field(description="The work")
    task: asyncio.Task[tuple[PassReport, ...]] = Field(description="The running loop")
    started_at: datetime = Field(default_factory=utc_now, description="When it began")
    finished_at: datetime | None = Field(default=None, description="When it stopped")
    passes: list[PassReport] = Field(
        default_factory=list, description="What each finished pass did"
    )
    stopped: bool = Field(default=False, description="Whether somebody stopped it")
    failure: str = Field(default="", description="What it raised, if it did")

    def status(self) -> WorkLoopStatus:
        """This loop as the browser reads it."""
        return WorkLoopStatus(
            work=self.work,
            running=not self.task.done(),
            started_at=self.started_at,
            finished_at=self.finished_at,
            passes=tuple(self.passes),
            stopped=self.stopped,
            failure=self.failure,
        )


class WorkLoopManager:
    """Every work with a loop over it in this process."""

    def __init__(self, sessions: SessionManager | None = None) -> None:
        self.loops: dict[str, WorkLoop] = {}
        # Every part run this manager starts is registered with the session
        # manager, so the pages that render a session render a part run too,
        # and a route can ask what one is doing. Optional because a test of
        # the loop's own bookkeeping has no browser to publish to.
        self.sessions = sessions
        self.watching = WatchedByTheBrowser(sessions) if sessions else PartRunWatch()

    def loop_for(self, work: str) -> WorkLoop | None:
        """The loop over one work, where this process is running one."""
        return self.loops[work] if work in self.loops else None

    def running(self, work: str) -> bool:
        """Whether a loop over this work is in flight."""
        held = self.loop_for(work)
        return held is not None and not held.task.done()

    def status(self, work: str) -> WorkLoopStatus:
        """Where one work's loop stands, for a work that has never had one too."""
        held = self.loop_for(work)
        return held.status() if held else WorkLoopStatus(work=work, running=False)

    def start(
        self,
        store: ManuscriptStore,
        work: str,
        manuscript: Manuscript,
        *,
        vocabulary: tuple[Abbreviation, ...] = (),
        passes: int,
        limit: int,
        only: tuple[str, ...] = (),
        concurrency: int,
        reconciling: bool,
    ) -> WorkLoopStatus:
        """Take this work to rest in the background, and hand back where it stands.

        Refuses where a loop is already in flight, because the loop owns the
        work's state and a second one would lease parts the first had leased.
        """
        if self.running(work):
            raise LoopAlreadyRunning(f"{work} already has a loop running")

        held = WorkLoop(
            work=work,
            task=asyncio.create_task(
                run_loop(
                    store,
                    work,
                    manuscript,
                    vocabulary=vocabulary,
                    passes=passes,
                    limit=limit,
                    only=only,
                    concurrency=concurrency,
                    reconciling=reconciling,
                    reporting=lambda report: self.recorded(work, report),
                    watching=self.watching,
                ),
                name=f"inkwell-work-loop-{work}",
            ),
        )
        self.loops[work] = held
        held.task.add_done_callback(lambda task: self.finished(work, task))
        logger.info("Loop over %s started: %d pass(es) at most", work, passes)
        return held.status()

    def recorded(self, work: str, report: PassReport) -> None:
        """Keep what a pass did, so a watcher hears it before the loop ends."""
        held = self.loop_for(work)
        if held is not None:
            held.passes.append(report)

    def finished(self, work: str, task: asyncio.Task[tuple[PassReport, ...]]) -> None:
        """Record how the loop ended, including the ways that are not success."""
        held = self.loop_for(work)
        if held is None:
            return
        held.finished_at = utc_now()
        if task.cancelled():
            held.stopped = True
            logger.info("Loop over %s was stopped", work)
            return
        failure = task.exception()
        if failure is not None:
            held.failure = str(failure) or failure.__class__.__name__
            logger.error("Loop over %s raised: %s", work, held.failure)

    async def stop(self, work: str) -> WorkLoopStatus:
        """Stop the loop over one work, and wait for it to actually be gone.

        Awaited rather than fired, so the reply cannot report a loop as stopped
        while its last part is still writing prose into the work.
        """
        held = self.loop_for(work)
        if held is None or held.task.done():
            return self.status(work)
        held.task.cancel()
        try:
            await held.task
        except asyncio.CancelledError:
            logger.info("Loop over %s ended on request", work)
        except Exception:
            logger.exception("Loop over %s raised as it was stopping", work)
        return self.status(work)

    async def shutdown(self) -> None:
        """Stop every loop this process is running, for a server going down."""
        for work in tuple(self.loops):
            await self.stop(work)
