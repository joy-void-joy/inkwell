"""The plan one part's run is handed, instead of the one it would derive.

A part run planned for itself: it opened the subsection it was holding, read
the sections that were there, and planned those. That is the cheapest possible
answer to "what should this part be" and it is answered by the wrong reader —
nothing inside a run that can see one subsection is in a position to say what
that subsection should open with, how much of its chapter it is entitled to, or
which of its claims are worth spending research on. Twenty-five of one run's
thirty research questions opened "The draft says…", which is what a planner
shown only the draft has to ask.

So the plan arrives from outside, composed by something that can see the part's
place in the work: its neighbours, its share of its chapter, the figures the
book numbers for it, what the research holds on its subject, and what asked for
the run. Every one of those is already assembled — the brief is what turns them
from a paragraph of instruction into the structure the write stage reads.

**It never opens the book.** The deriver reads one part, the two files either
side of it, and this part's slice of what the corpus established. That bound is
deliberate and it is what keeps a single part affordable: a producer that read
the whole work to plan one subsection would make testing one subsection cost a
book read. The other producer of the same artifact — a planner that reads the
whole work and emits every part's brief at once — sees cross-cutting staleness
this cannot, and is what a full pass wants. Both emit an ``ArticlePlan``, so
nothing downstream knows which one it got.

**The plan stage is not skipped; it is answered.** A run handed a brief records
it and moves on — the Plan tab is still written, the section tabs still created,
the overview still updated. Skipping the stage would have saved the same money
and silently taken all of that with it.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from inkwell.agent.client import query
from inkwell.agent.config import stage_model
from inkwell.agent.models import (
    ArticlePlan,
    ResearchQuestion,
    SectionPlan,
    SourceQuote,
)
from inkwell.manuscript.budget import LengthBudget
from inkwell.manuscript.findings import WorkFindings, briefing
from inkwell.manuscript.inventory import Figure
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

logger = logging.getLogger(__name__)

BRIEF_FILE = "brief.json"
"""What the plan handed to a run is called, in the run's own room.

Kept beside the drafts rather than only inside the run's artifacts, because the
question afterwards is usually whether the brief was wrong rather than whether
the writing was: a part that came back at four times its length either ignored
its budget or was never given one, and only this file says which.
"""

BRIEF_STAGE = "plan"
"""Which stage's model tier composing a brief runs at.

The stage it replaces. It is doing that stage's work from better inputs, not
cheaper work — the saving is that it reads a subsection and its surroundings
rather than a subsection and a research compilation, not that it thinks less.
"""

BRIEF_SYSTEM = """\
You are planning one part of a book that already exists.

What you produce is the plan its writer works from. The writer will see the \
part as the book currently holds it and will write a successor to it — so your \
plan decides what that successor *is*, and the current text's own section order \
is evidence about the subject rather than the shape of your answer.

Three things are settled before you start, and you are not deciding them:

- **The length budget.** It is stated in your task and it is the part's share \
of its chapter. Plan sections that fit it. If what has changed genuinely needs \
more, say so in the author direction and plan for what it needs — but a plan \
that quietly runs to several times the budget is a plan that has decided the \
book's shape.
- **The figures.** They carry the numbers the book gave them and they are \
reproduced as they stand. Plan where each one goes; never renumber one.
- **What licenses the scope.** The reasons this part is being revised are in \
your task. A reason naming a claim asks for that claim to be settled; a reason \
about what the part is *for* licenses reshaping it. Where nothing asks for a \
change, the part's argument is the best evidence of what it establishes, and \
your plan should arrive at something recognisably the same piece.

**Research questions are about the subject, not about the draft.** A question \
that opens "The draft says…" can only ever confirm what is already written, and \
a plan made of those has a blind spot shaped exactly like everything the draft \
left out. Ask what a well-read reader would notice missing, what has happened \
that a piece written now has to account for, and which of the part's \
load-bearing claims would embarrass the book if they had dated. Where the \
research findings in your task already answer a question, do not ask it — say \
in the section's key points that the finding settles it.

Be specific and be brief. Title and summary is enough per section; one or two \
key points at most.
"""


class ComposedBrief(BaseModel):
    """What the deriver answers with, before it is a plan the pipeline reads.

    Its own shape rather than an ``ArticlePlan`` because most of that model is
    not the deriver's to decide: the format comes from the work, the placement
    from the launch, the voice from the author. Asking for those back would be
    asking a reader to restate what it was told, which is a way of getting a
    different answer.
    """

    thesis: str = Field(
        description="What this part establishes, in one specific sentence"
    )
    sections: list[SectionPlan] = Field(
        default=[], description="The successor's sections, in reading order"
    )
    research_questions: list[ResearchQuestion] = Field(
        default=[],
        description="What has to be settled before this can be written — about "
        "the subject, never about what the current text happens to say",
    )
    source_quotes: list[SourceQuote] = Field(
        default=[],
        description="Passages of the current text worth preserving verbatim",
    )
    direction: str = Field(
        default="",
        description="What the writer most needs to know about this part that "
        "the sections do not say — including where the budget has to give",
    )
    constraints: list[str] = Field(
        default=[],
        description="Short checkable items the successor must satisfy",
    )


class BriefWriter(ABC):
    """Who composes the plan one part's run is handed.

    A seam, only ever injected: nothing here constructs one for itself, so a
    test hands over a writer that answers from the fixture and a run hands over
    one that spends a model.
    """

    @abstractmethod
    async def compose(self, task: str, root: Path) -> ComposedBrief | None:
        """Plan one part, or return None where it could not."""


class ModelBrief(BriefWriter):
    """One plan, bought at the tier the stage it replaces runs at."""

    def __init__(self, model: str = "") -> None:
        self.model = model

    async def compose(self, task: str, root: Path) -> ComposedBrief | None:
        return await query(
            task,
            output_type=ComposedBrief,
            model=self.model or stage_model(BRIEF_STAGE),
            system_prompt=BRIEF_SYSTEM,
            tools=["Read", "Grep", "Glob"],
            autonomy="unattended",
            prefix="brief",
            cwd=root,
        )


def asked(
    manuscript: Manuscript,
    node: ManuscriptNode,
    material: Path,
    instruction: str,
    research: str,
) -> str:
    """What the deriver is shown: one part, its surroundings, and what asked.

    The instruction the run would have been given anyway, plus the material as
    a file it can open. Nothing is restated: what the writer will be told and
    what the planner is told are the same paragraph, so a plan cannot be made
    against a brief the writer never sees.
    """
    return "\n\n".join(
        held
        for held in (
            f"# Planning {node.key} — {node.title}, of {manuscript.title}",
            f"The part as the book holds it now: {material}\nRead it first.",
            instruction,
            research,
            "Plan the successor.",
        )
        if held
    )


def planned(
    composed: ComposedBrief,
    manuscript: Manuscript,
    node: ManuscriptNode,
    figures: tuple[Figure, ...],
    budget: LengthBudget,
) -> ArticlePlan:
    """The deriver's answer as the plan every stage after it reads.

    The fields the deriver was not asked for are filled from what already knows
    them — the work says the format, the node says the title — rather than from
    a second reading. ``deliverables`` is the contract every stage is measured
    on, so what a part run owes is stated here once: this part, its heading, its
    figures, and its length.
    """

    def owed() -> Iterator[str]:
        """What this run has to hand back, whatever else it does."""
        yield f"the part {node.title!r} alone, opening with its own heading"
        if figures:
            yield (
                f"every one of its {len(figures)} figure(s), reproduced as the "
                f"book numbers them: {', '.join(one.render() for one in figures)}"
            )
        if budget.holds:
            yield (
                f"a piece between roughly {budget.floor():,} and "
                f"{budget.ceiling():,} words"
            )

    return ArticlePlan(
        title=node.title,
        thesis=composed.thesis,
        target_format=manuscript.target_format,
        sections=composed.sections,
        research_questions=composed.research_questions,
        source_quotes=composed.source_quotes,
        author_direction=composed.direction,
        constraints=composed.constraints,
        deliverables=list(owed()),
        conventions=[],
        voice_notes="",
    )


async def compose(
    manuscript: Manuscript,
    node: ManuscriptNode,
    material: Path,
    room: Path,
    *,
    instruction: str,
    figures: tuple[Figure, ...] = (),
    budget: LengthBudget | None = None,
    found: WorkFindings | None = None,
    writer: BriefWriter | None = None,
) -> ArticlePlan | None:
    """Plan one part from its place in the work, and keep the plan.

    ``None`` where the deriver came back with nothing, which is a run that
    plans for itself as it always did rather than a run that fails: a brief is
    a better plan, not a required one, and losing a book pass to a planner that
    timed out would be the expensive way to hold that opinion.
    """
    composed = await (writer if writer is not None else ModelBrief()).compose(
        asked(
            manuscript,
            node,
            material,
            instruction,
            briefing(found, node.key) if found is not None else "",
        ),
        Path(manuscript.root),
    )
    if composed is None:
        logger.warning(
            "No brief was composed for %s — it will plan for itself", node.key
        )
        return None

    held = planned(composed, manuscript, node, figures, budget or LengthBudget())
    (room / BRIEF_FILE).write_text(held.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "Brief for %s: %d section(s), %d research question(s)",
        node.key,
        len(held.sections),
        len(held.research_questions),
    )
    return held
