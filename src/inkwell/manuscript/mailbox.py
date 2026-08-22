"""What a part's run could not settle for itself, and who answers it.

A run revising one part of somebody's book reaches things it cannot decide.
Some are facts, and those need no protocol at all: an agent reads the file. A
question is what is left when reading cannot answer — what the authors intend,
which of two framings the book is committed to, whether a claim that has dated
should be updated or cut. Those go to whoever owns the book.

**Asking parks the part; it does not fail it.** A parked part is sound work
suspended, and the difference matters to everything upstream: a failed part is
retried and fails again, a parked one waits and costs nothing until an answer
arrives. The standing carries that distinction, so a status display and a
scheduler both read it without either being told.

**A question escalates rather than blocking.** A part cannot answer its
sibling's question, so a question asked of a subsection is addressed to the
part above it — its section, then its chapter, then the work — and the first
level that can answer does. That is the tree already in hand being used for
what a tree is for, and it is why nothing here needs a routing table.

**Built on lup's channel slots, not on its resolver.** ``Slot`` and
``SlotSet`` are the file-backed, settle-once-by-exclusive-create machinery
this needs, and they are generic over any model. The resolver's own question
type is not reused: it carries concern ids, edit-gate allowances, and lost
criteria, none of which mean anything about a book, and adopting it would put
this module in the resolver's vocabulary rather than the work's.
"""

import logging
from collections.abc import Iterator
from datetime import datetime
from hashlib import blake2b
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from lup.channels.models import utc_now
from lup.channels.slot import SlotSet

from inkwell.manuscript.store import flattened
from inkwell.manuscript.tree import Manuscript

logger = logging.getLogger(__name__)

QUESTION_DIR = "questions"
"""Where a work's open questions sit, one directory per question."""


class PartQuestion(BaseModel):
    """One thing a part's run needs answered before it can finish.

    Addressed to a part rather than to a person: who answers is whoever is
    watching the work, and naming an individual here would be this system
    deciding a book's editorial arrangement.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="What this question is addressed by")
    work: str = Field(description="The work it is about")
    asker: str = Field(description="Key of the part whose run asked")
    addressed_to: str = Field(
        default="",
        description="Key of the part it escalated to, empty where it reached "
        "the work itself",
    )
    prompt: str = Field(description="The question, in the run's own words")
    session: str = Field(default="", description="The run that asked")
    asked_at: datetime = Field(default_factory=utc_now, description="When")


class PartAnswer(BaseModel):
    """What somebody said, written once and never revised.

    Never revised because the part was rebuilt against it: an answer that
    could change under a finished run would leave the work built on something
    nobody said. A corrected answer is a new question.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The question this settles")
    value: str = Field(description="The answer, in the answerer's own words")
    answered_by: str = Field(default="", description="Who answered")
    answered_at: datetime = Field(default_factory=utc_now, description="When")


class MailboxRecord(BaseModel):
    """One slot's contents: the question, and the answer where there is one.

    Both in one record because the slot machinery addresses one model, and
    holding them together is what lets a reader see an answered question
    without a second lookup.
    """

    model_config = ConfigDict(frozen=True)

    question: PartQuestion | None = Field(
        default=None, description="What was asked, once somebody asked"
    )
    answer: PartAnswer | None = Field(
        default=None, description="What was said, once somebody said it"
    )


def question_id(key: str, prompt: str) -> str:
    """What one question is addressed by: its part, and the words it asked.

    Derived rather than handed out, so a run that asks the same thing twice —
    the same part, re-run after a failure — reaches the slot it already has
    instead of filling the work with duplicates of one question. The prompt is
    hashed rather than spelled because it is a sentence and this is a
    directory name.
    """
    digest = blake2b(prompt.strip().encode("utf-8"), digest_size=8).hexdigest()
    return f"{flattened(key)}.{digest}"


def escalated_to(manuscript: Manuscript, key: str) -> str:
    """The part a question from ``key`` is addressed to.

    Its nearest ancestor, which is what "escalate up the tree" means with a
    key that is already a path. A part with no ancestor — a chapter, or a
    work that is one flat list — addresses the work itself, and that is the
    empty answer rather than a special one.
    """
    ancestors = [
        node.key
        for node in manuscript.walk()
        if key.startswith(f"{node.key}/") and node.key != key
    ]
    return max(ancestors, key=len) if ancestors else ""


class PartMailbox(BaseModel, frozen=True):
    """One work's open questions, file-backed under its own record."""

    root: Path

    def slots(self) -> SlotSet[MailboxRecord]:
        """The slot set every question is addressed through."""
        return SlotSet(self.root / QUESTION_DIR, MailboxRecord)

    def ask(self, question: PartQuestion) -> str:
        """Record one question, or find it already asked and leave it alone.

        Re-asking is a no-op rather than an error: a part re-run after a
        failure asks what it asked before, and treating that as a conflict
        would fail the retry for doing exactly the right thing.
        """
        slot = self.slots().slot(question.id)
        held = slot.declared()
        if held is not None and held.question is not None:
            return question.id
        slot.declare(MailboxRecord(question=question))
        return question.id

    def answer(self, settled: PartAnswer) -> bool:
        """Settle one question, or report that somebody already did."""
        slot = self.slots().slot(settled.id)
        held = slot.declared()
        if held is None or held.question is None:
            raise KeyError(f"no question is addressed by {settled.id!r}")
        return slot.settle(MailboxRecord(question=held.question, answer=settled))

    def settled(self, identifier: str) -> PartAnswer | None:
        """The answer to one question, where somebody has given one."""
        held = self.slots().slot(identifier).settled()
        return None if held is None else held.answer

    def open(self) -> tuple[PartQuestion, ...]:
        """Every question still waiting, which is what parks the parts."""

        def waiting() -> Iterator[PartQuestion]:
            """Each declared question no answer has settled."""
            held = self.slots()
            for name in held.names():
                slot = held.slot(name)
                declared = slot.declared()
                if declared is not None and declared.question is not None:
                    if slot.settled() is None:
                        yield declared.question

        return tuple(waiting())

    def asked_by(self, key: str) -> tuple[PartQuestion, ...]:
        """What one part is waiting on, which is why it is parked."""
        return tuple(held for held in self.open() if held.asker == key)

    def answers_for(self, key: str) -> tuple[PartAnswer, ...]:
        """Every answer one part asked for and has since been given.

        What a re-run is handed. Ordered oldest first, because a part that
        asked three questions across two attempts reads them the way it asked
        them.
        """
        held = self.slots()

        def given() -> Iterator[PartAnswer]:
            """Each settled slot this part is the asker of."""
            for name in held.names():
                record = held.slot(name).settled()
                if (
                    record is not None
                    and record.answer is not None
                    and record.question is not None
                    and record.question.asker == key
                ):
                    yield record.answer

        return tuple(sorted(given(), key=lambda answer: answer.answered_at))
