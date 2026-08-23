"""What a work has left to do, and how a change reaches the parts that care.

This is the build system's dependency pass. It reads every part as the work
holds it now, works out what each one depends on, and asks the state whether
each is out of date — nothing here is stored, because everything here can be
asked again.

**Adoption is why this is affordable.** A work of two hundred parts where
nothing has been built is a work where every part is out of date, and a loop
that took that literally would rewrite the whole book before it could tell
anybody anything. So a work can be *adopted*: every part stamped as built from
the text it already holds, with the dependencies that text already implies.
That is ``make -t`` and it is the same argument — the targets are up to date,
they were simply built by hand, and declaring so is what turns the first
useful pass from a rewrite of everything into a rewrite of what somebody
actually asked about. The full pass over every part stays available and stays
expensive; it stops being the entry fee.

**Propagation is a fixed point, and it is reached by not moving.** A part is
reached only where what it consumed intersects what some run changed, so a
rewrite that redefines nothing reaches nothing however many parts sit
downstream of it. Two chapters that each depend on the other settle for the
same reason rather than in spite of it: the cycle carries change facts, and
when the rewrites stop producing facts the cycle carries nothing.
"""

import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkwell.manuscript.facts import (
    Consumption,
    Dependency,
    bears_on,
    consumption_of,
)
from inkwell.manuscript.links import links_from
from inkwell.manuscript.splice import held_text
from inkwell.manuscript.state import (
    UNRUNNABLE_STALENESS,
    NodeVerdict,
    PartText,
    Staleness,
    WorkState,
)
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.manuscript.vocabulary import Abbreviation, terms_used

logger = logging.getLogger(__name__)


class PartReading(BaseModel):
    """One part of a work as it stands: its node, its text, what it leans on.

    The three things every question in this module is answered from, read
    together so a sweep opens each file once however many parts share it.
    """

    model_config = ConfigDict(frozen=True)

    node: ManuscriptNode = Field(description="The part")
    text: str = Field(description="What it holds now")
    found: bool = Field(
        default=True,
        description="Whether the work still holds that text where it was imported "
        "from. False for a part whose file or heading is not there",
    )
    consumed: Consumption = Field(
        default_factory=Consumption,
        description="What its text implies it depends on, before any run has said",
    )

    def part(self) -> PartText:
        """This reading as the state judges it."""
        return PartText(key=self.node.key, text=self.text, found=self.found)


def source_text(
    root: Path,
    node: ManuscriptNode,
    # lup: ignore[dict-str-payload] — a cache keyed by whatever paths a work has
    opened: dict[str, str | None],
) -> str | None:
    """One part's text, reading each file at most once across a whole sweep.

    Seven subsections sharing a section file would otherwise read it seven
    times, and a work of two hundred parts is read in a loop that runs on
    every status check.

    ``None`` where the part's text is not there to read — the file is missing,
    or the heading it begins at is no longer in that file. Distinct from the
    empty string, which is a part the work declares no text for and a part an
    author emptied, because those need no repair and a missing file does.
    """
    if not node.path:
        return ""
    if node.path not in opened:
        target = root / node.path
        opened[node.path] = (
            target.read_text(encoding="utf-8") if target.is_file() else None
        )
    source = opened[node.path]
    if source is None or not node.heading:
        return source
    # A found span always carries its own heading line, so empty says the
    # heading is not in the file rather than that the part has no prose.
    return held_text(source, node) or None


def missing_root(manuscript: Manuscript) -> Path | None:
    """The directory the work was read from, where it is no longer there.

    Asked before a sweep is reported, because the answer changes what the sweep
    *means*: every part of a work whose checkout has moved reads as having lost
    its text, and two hundred parts each saying so is a worse account of one
    missing directory than one line naming it. ``None`` where the root is
    present and the verdicts stand on their own.
    """
    root = Path(manuscript.root)
    return None if root.is_dir() else root


def readings(
    manuscript: Manuscript, vocabulary: Iterable[Abbreviation] = ()
) -> tuple[PartReading, ...]:
    """Every part a run can be about, with its text and its derived dependencies.

    Derived rather than recorded, because a work that has never been run has
    no record and is exactly the case that most needs an answer. What a run
    later reports supersedes this for that part — it knows what it actually
    reached for, where this knows only what the prose implies.

    Every part depends on what the research holds about its own subject, and
    that one is derived rather than reported for a reason the others are not:
    a run that found no research to read would report no dependency on it, and
    the part would then never hear about a paper published afterwards — which
    is exactly the part most in need of hearing.
    """
    root = Path(manuscript.root)
    declared = tuple(vocabulary)
    # lup: ignore[dict-str-payload] — a cache keyed by whatever paths a work has
    opened: dict[str, str | None] = {}

    def read() -> Iterator[PartReading]:
        """Each leaf paired with what it holds and what that implies."""
        for node in manuscript.leaves():
            held = source_text(root, node, opened)
            text = held or ""
            yield PartReading(
                node=node,
                text=text,
                found=held is not None,
                consumed=consumption_of(
                    (
                        bears_on(node.key),
                        *terms_used(text, declared),
                        *links_from(manuscript, node, text),
                    )
                ),
            )

    return tuple(read())


class WorkSweep(BaseModel):
    """What one pass over a work found, part by part.

    Carries every verdict rather than only the outstanding ones: a display
    that showed only what is dirty could not tell a work that is finished from
    a work nobody has imported.
    """

    verdicts: tuple[NodeVerdict, ...] = Field(
        default=(), description="One verdict per part, in reading order"
    )

    def dirty(self) -> tuple[NodeVerdict, ...]:
        """Every part with work outstanding, which is what a pass has left."""
        return tuple(held for held in self.verdicts if held.dirty())

    def blocked(self) -> tuple[NodeVerdict, ...]:
        """Every part outstanding that no run can put right.

        Reported apart from the rest because the repair is somebody else's: a
        work read from a checkout that is no longer there is outstanding in
        every part and runnable in none, and a caller that only asked what was
        schedulable would call that settled.
        """
        return tuple(
            held for held in self.verdicts if held.staleness in UNRUNNABLE_STALENESS
        )

    def settled(self) -> bool:
        """Whether the work is at rest, which is what ends a pass."""
        return not self.dirty()

    def counted(self) -> dict[Staleness, int]:
        """How many parts stand each way, for a status line to render."""
        kinds: list[Staleness] = [held.staleness for held in self.verdicts]
        return {kind: kinds.count(kind) for kind in dict.fromkeys(kinds)}


def sweep(state: WorkState, held: Iterable[PartReading]) -> WorkSweep:
    """Judge every part of a work against what it holds now."""
    return WorkSweep(verdicts=tuple(state.verdicts(reading.part() for reading in held)))


def adopted(state: WorkState, held: Iterable[PartReading], run: str = "") -> WorkState:
    """This state with every part stamped as built from what it already holds.

    What makes an existing work joinable. Only parts with no stamp are
    touched: a part something has already built has a record of what that run
    consumed, and overwriting it with what the prose merely implies would
    discard the better answer for the worse one.
    """
    adopting = [reading for reading in held if state.stamp(reading.node.key) is None]
    for reading in adopting:
        state = state.built(
            reading.node.key,
            source=reading.text,
            consumed=reading.consumed,
            run=run,
        )
    logger.info("Adopted %d part(s) as built from the text they hold", len(adopting))
    return state


def consumers(
    held: Iterable[PartReading], dependency: Dependency
) -> tuple[ManuscriptNode, ...]:
    """Every part that leans on one thing, which is what a change would reach.

    Asked of the readings rather than of the state so it answers for a work
    nothing has run against — what a change *would* reach is the question an
    author asks before deciding to make it.
    """
    return tuple(
        reading.node for reading in held if reading.consumed.touches(dependency)
    )
