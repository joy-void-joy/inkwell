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

from inkwell.manuscript.loop import PassReport, run_loop
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript
from inkwell.manuscript.vocabulary import Abbreviation

logger = logging.getLogger(__name__)


class LoopAlreadyRunning(Exception):
    """Raised where a work already has a loop over it."""


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

    def __init__(self) -> None:
        self.loops: dict[str, WorkLoop] = {}

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
