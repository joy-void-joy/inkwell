"""What is being done to a work, at the two levels that are not a part.

A pass over a book has three levels and only one of them was visible. The pass
itself reports when it is over; each part's run reports through the work's state
while it holds a lease; and beneath that, the agents — nine writers, six
reviewers, a distiller per document — reported nowhere anything outside the run
could read.

Two of those three are already answered elsewhere. This is for the ones that
belong to no part at all: distilling a document, placing findings on a tree,
checking a reference, composing a brief. A pass that spends two hundred dollars
reading documents before it writes a word is doing the most expensive thing it
will do all day, and with nothing recording it the work looks idle — which is
the state somebody stops a loop out of.

**Recorded where the work is, not where the run is.** A part run's agents live
under that run's own artifacts and die with it, which is right: they are that
run's. These outlive every run — a distillation is cached for good and a
reference verdict is shared across the book — so they are recorded against the
work, and any process that can read the work can read them.

**Opened and closed, never counted.** A record that only counted completions
would show nothing at all during the hours it takes, which is exactly the window
somebody is asking about. So an agent is written down when it opens and amended
when it closes, and something reading mid-pass sees what is running.
"""

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lup.channels.models import publish_atomic, utc_now

logger = logging.getLogger(__name__)

ATTENDANCE_FILE = "attending.json"
"""Where the agents a work spends outside any part are recorded."""


class WorkAgent(BaseModel):
    """One agent working on a work rather than on a part of it.

    ``about`` is what it is reading rather than what it is doing, because that
    is the half a reader needs: "distilling" tells somebody a stage is running,
    and "distilling *Pre-deployment evaluations*" tells them how far through
    three hundred documents it is.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(
        description="What this agent is addressed by, stable across a re-run so "
        "an agent opened twice replaces its record rather than doubling it"
    )
    kind: str = Field(description="What it is — distil, assign, reference, brief")
    about: str = Field(default="", description="What it is reading or placing")
    part: str = Field(
        default="",
        description="The part it belongs to, empty for work-level work — which "
        "is what this record exists for",
    )
    opened_at: datetime = Field(default_factory=utc_now, description="When it started")
    closed_at: datetime | None = Field(
        default=None, description="When it finished, absent while it is going"
    )
    detail: str = Field(default="", description="What it found, once it has")
    failure: str = Field(
        default="", description="Why it produced nothing, where it did"
    )

    def running(self) -> bool:
        """Whether this agent is still working."""
        return self.closed_at is None

    def render(self) -> str:
        """This agent as a line somebody watching a pass reads."""
        placed = f" — {self.about}" if self.about else ""
        said = self.failure or self.detail
        return f"{self.kind}{placed}{f': {said}' if said else ''}"


class Attendance(BaseModel):
    """Every agent a work has spent outside its parts, oldest first."""

    agents: tuple[WorkAgent, ...] = Field(
        default=(), description="One entry per agent, opened or closed"
    )

    def live(self) -> tuple[WorkAgent, ...]:
        """Every agent still working, which is what a pass looks idle without."""
        return tuple(one for one in self.agents if one.running())

    def of_kind(self, kind: str) -> tuple[WorkAgent, ...]:
        """Every agent of one kind, for a reader asking about one stage."""
        return tuple(one for one in self.agents if one.kind == kind)

    def with_agent(self, held: WorkAgent) -> "Attendance":
        """This record with one agent replaced whole, keeping its place.

        Replaced rather than appended, so an agent that opens and then closes
        is one row rather than two, and one re-run after a failure is one row
        rather than a history nobody asked for. Its place is kept because the
        order agents opened in is the order a reader follows them in.
        """
        if not any(one.id == held.id for one in self.agents):
            return self.model_copy(update={"agents": (*self.agents, held)})
        return self.model_copy(
            update={
                "agents": tuple(
                    held if one.id == held.id else one for one in self.agents
                )
            }
        )

    def closing(
        self, identifier: str, *, detail: str = "", failure: str = ""
    ) -> "Attendance":
        """This record with one agent marked finished.

        An id nothing opened is ignored rather than raising: this is a record
        of what happened, and a close that arrives for an agent nobody wrote
        down is a lost open, not a reason to fail the pass that did the work.
        """
        found = next((one for one in self.agents if one.id == identifier), None)
        if found is None:
            logger.debug("Closing %s, which nothing opened", identifier)
            return self
        return self.with_agent(
            found.model_copy(
                update={"closed_at": utc_now(), "detail": detail, "failure": failure}
            )
        )

    def rendered(self) -> Iterator[str]:
        """Each agent as a line, the ones still working first."""
        for one in (
            *self.live(),
            *(held for held in self.agents if not held.running()),
        ):
            yield one.render()


class AttendanceLog(BaseModel, frozen=True):
    """One work's attendance on disk, written as each agent opens and closes.

    File-backed rather than held in memory for the reason the roster is: the
    question "what is this pass doing right now" is asked from a browser in
    another process, and an answer only the pass itself holds cannot be given.

    Each write is whole and atomic, so a reader sees one complete record or the
    one before it. The writers here are sequential within a pass — a callback
    per document, on the pass's own loop — so nothing needs a lock.
    """

    path: Path

    def load(self) -> Attendance:
        """What is recorded, empty where nothing is.

        An unreadable file reads as empty: it is a record of activity rather
        than of the book, and losing it costs a display rather than the work.
        """
        if not self.path.is_file():
            return Attendance()
        try:
            return Attendance.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            logger.warning("Unreadable attendance at %s", self.path, exc_info=True)
            return Attendance()

    def publish(self, held: Attendance) -> Path:
        """Write the record, atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        publish_atomic(self.path, held)
        return self.path

    def opening(self, agent: WorkAgent) -> None:
        """Record that one agent has started."""
        self.publish(self.load().with_agent(agent))

    def closed(self, identifier: str, *, detail: str = "", failure: str = "") -> None:
        """Record that one agent has finished."""
        self.publish(self.load().closing(identifier, detail=detail, failure=failure))

    def clear(self) -> None:
        """Forget every agent, for a work starting a pass afresh.

        Offered rather than done automatically. A record that cleared itself at
        the start of a pass would erase the account of the pass somebody is
        still reading about, and one that never cleared would grow for as long
        as the book is written.
        """
        self.publish(Attendance())


def attending(root: Path) -> AttendanceLog:
    """The attendance record under one work's directory."""
    return AttendanceLog(path=root / ATTENDANCE_FILE)


def spent_on(agents: Iterable[WorkAgent], kind: str) -> int:
    """How many agents of one kind a work has opened."""
    return sum(1 for one in agents if one.kind == kind)
