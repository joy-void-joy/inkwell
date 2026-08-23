"""Planning every outstanding part of a work at once, from above all of them.

The other producer of the same artifact :mod:`.brief` makes one at a time. Both
emit an ``ArticlePlan`` per part, so nothing downstream knows which one it got,
and that is the point: a run testing one subsection derives its own brief and
never opens the book, while a full pass plans from a reader that has.

**What only this can see is what two parts are about to do to each other.** A
per-part deriver reading one subsection and its neighbours cannot notice that
three parts have all just been handed the same paper and are each about to
introduce it, or that the part being asked to establish something is the third
part in reading order to be asked to establish it. Those are the findings the
run-level reviewers cannot reach either — a reviewer reads one part and asks
whether it is good — and they cost a book read, which is why they are a full
pass's to pay for and not a single part's.

**A cross-cutting reading, then a chapter at a time.** The first pass reads the
outline and everything outstanding and says what the parts are about to do to
each other; the chapters are then planned against that. One call emitting two
hundred briefs would be one call to lose, and a chapter is the unit whose parts
actually share anything — the cross-cutting reading is what carries the rest.

**Nothing here decides what is outstanding.** The sweep does, as it does for
everything: this plans the parts it is handed. A planner that judged which parts
needed work would be a second answer to the question the ledger already
answers, and two answers that disagree is a loop that never settles.
"""

import asyncio
import logging
from collections.abc import Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.client import query
from inkwell.agent.config import stage_model
from inkwell.agent.models import ArticlePlan
from inkwell.corpus.storage import now_stamp
from inkwell.manuscript.brief import BRIEF_STAGE, ComposedBrief, planned
from inkwell.manuscript.budget import budget_for
from inkwell.manuscript.findings import WorkFindings, briefing, outlined
from inkwell.manuscript.inventory import inventory_of
from inkwell.manuscript.state import NodeVerdict, digest_of
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

logger = logging.getLogger(__name__)

CROSSCUT_SYSTEM = """\
You are reading a whole book before a pass rewrites part of it, and you are \
looking for exactly one thing: what the outstanding parts are about to do to \
each other.

You are given the book's structure, the parts that are outstanding and why, and \
what the research has newly established about each of them. Report only what no \
reader of a single part could see:

- **The same material arriving twice.** Three parts handed the same paper are \
three parts each about to introduce it. Say which one should, and what the \
others should do instead — refer to it, or leave it alone.
- **An establishment ordered wrongly.** A part being asked to define something \
a later part already defines, or to lean on something no earlier part has \
established yet. Reading order is in the structure you were given.
- **A claim being changed in one place and not another.** Where one part is \
about to revise a figure or an attribution that other parts also carry, name \
the others: they are wrong the moment the first one lands.
- **Scale that only the book can judge.** A part whose outstanding work would \
plainly make it several times its siblings, and what should give.

Report nothing else. "This part could be clearer" is a reviewer's finding and \
this is not that pass; you are the only reader who will see the book at once, \
so spend it on what needs the book.
"""

PLANNER_SYSTEM = """\
You are planning several parts of a book at once, all of them in one chapter.

For each part you are given: the part as the book holds it now, why it is \
outstanding, what the research holds on it, the figures it carries with the \
numbers the book gave them, and its length budget. You are also given what a \
reader of the whole book said these parts are about to do to each other — that \
reading is the reason you are planning them together rather than one at a time, \
so where it names one of your parts, your plan for that part answers it.

Plan each part's successor: its thesis, its sections, and what has to be \
settled before it can be written. Three things are settled before you start and \
are not yours to decide — the length budget, the figure numbering, and what the \
reasons license. Where nothing asks a part to change, plan something \
recognisably the same piece.

**Research questions are about the subject, not about the draft.** A question \
opening "The draft says…" can only confirm what is already written, and a plan \
made of those has a blind spot shaped exactly like everything the draft left \
out. Where a finding you were given already answers a question, do not ask it.

Return one plan per part, each naming the part by the key it was given under. A \
key you invent plans nothing.
"""


class BriefLens(BaseModel):
    """One independent question asked of every proposed brief."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Stable name persisted with this lens's concerns")
    system: str = Field(description="The independent reading this lens performs")


LENS_SYSTEMS: tuple[BriefLens, ...] = (
    BriefLens(
        name="ledger",
        system="""\
You are the scope reader in an adversarial review of plans for existing book \
parts. Read each proposed brief against the reasons the build ledger says its \
part is outstanding. Report only work the brief asks for that those reasons do \
not license, or a reason the brief fails to answer. A reason about one claim \
licenses settling that claim, not redesigning the whole part. A reason about \
what the part is for may license reshaping it. Name the part by its exact key. \
If the brief and ledger agree, report nothing for it.
""",
    ),
    BriefLens(
        name="standing-text",
        system="""\
You are the inheritance reader in an adversarial review of plans for existing \
book parts. Compare every proposed brief with the standing text. Report where \
the plan presents something the part already establishes as new work, silently \
drops a load-bearing claim, case, caveat, citation, or figure, or inherits the \
standing section order without deciding that it is still the right order. Do \
not demand that the old shape survive: identify the material that must survive \
or be deliberately retired. Name the part by its exact key. Report nothing \
where the plan already accounts for it.
""",
    ),
    BriefLens(
        name="neighbours",
        system="""\
You are the boundary reader in an adversarial review of plans for existing book \
parts. Read each proposed brief against the parts immediately before and after \
it. Report where it would repeat what a neighbour already establishes, rely on \
something a neighbour does not establish, or take work that belongs at that \
boundary. Repetition can be deliberate in a textbook, so report only a real \
ownership or dependency mistake. Name the part by its exact key. Report nothing \
where the boundary is sound.
""",
    ),
    BriefLens(
        name="sources",
        system="""\
You are the evidence reader in an adversarial review of plans for existing book \
parts. Look for a load-bearing claim the proposed successor would change or add \
on the strength of a single source, especially where the standing text or the \
research carries a caveat or contrary source. Report the claim and what the \
plan must settle before relying on it. Do not demand several citations for a \
fact one authoritative primary source settles, and do not assess prose style. \
Name the part by its exact key. Report nothing where the evidence is adequate.
""",
    ),
)
"""The independent readings every proposed brief receives.

The names are data because they are kept with the findings. A concern recorded
only as prose cannot later answer which question the reviewer was asking, and
adding a lens should be one tuple rather than another orchestration branch.
"""

LENS_REPAIR_SYSTEM = """\
You are settling plans for existing parts of a book after independent,
adversarial readers examined them.

Revise each proposed brief only as needed to answer the review findings. The
findings are objections, not instructions from the author: resolve them using
the standing text, the build-ledger reasons, the neighbouring parts and the
research shown in the task. Preserve a sound plan when an objection is
mistaken. Return one plan for every key you were given and invent no keys.
"""


class CrossCutting(BaseModel):
    """One thing the outstanding parts are about to do to each other."""

    model_config = ConfigDict(frozen=True)

    parts: list[str] = Field(
        default=[], description="Keys of the parts this is about, in reading order"
    )
    finding: str = Field(description="What they are about to do to each other")
    settle: str = Field(
        default="", description="What each of them should do about it instead"
    )

    def render(self) -> str:
        """This as a line a chapter's planning is handed."""
        named = ", ".join(self.parts) or "the work"
        said = f" — {self.settle}" if self.settle else ""
        return f"{named}: {self.finding}{said}"


class CrossCut(BaseModel):
    """What one reading of the whole book found across its outstanding parts."""

    findings: list[CrossCutting] = Field(
        default=[], description="One entry per thing no single part could see"
    )

    def touching(self, key: str) -> tuple[CrossCutting, ...]:
        """Everything said about one part, including what it shares."""
        return tuple(one for one in self.findings if key in one.parts)

    def render(self, keys: Iterable[str] = ()) -> str:
        """The findings as a chapter's planning reads them.

        Narrowed to the parts being planned where any are named, because a
        chapter shown every finding about the book would be shown mostly
        findings about parts it is not planning — and the ones that matter to
        it would be the hardest to see.
        """
        wanted = tuple(keys)
        held = (
            self.findings
            if not wanted
            else [
                one for one in self.findings if any(key in wanted for key in one.parts)
            ]
        )
        if not held:
            return ""
        return "## What these parts are about to do to each other\n\n" + "\n".join(
            f"- {one.render()}" for one in held
        )


class PartPlan(BaseModel):
    """One part's brief, as the planner names it."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part, by the key it was given under")
    brief: ComposedBrief = Field(description="What its successor should be")
    digest: str = Field(
        default="",
        description="Fingerprint of the part's text as the planner read it. A "
        "brief is a plan for a successor to *that* text, so once the part has "
        "been rewritten the brief is a plan for a piece that no longer exists",
    )


class ChapterPlans(BaseModel):
    """Every plan one chapter's planning produced."""

    plans: list[PartPlan] = Field(default=[], description="One entry per part planned")


class ProposedConcern(BaseModel):
    """One fault a lens believes a proposed brief has.

    The lens name is attached by the caller rather than trusted to the model:
    it is a fact about which independent reading produced the concern.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part whose brief is at fault")
    finding: str = Field(description="The concrete fault in the proposed brief")
    settle: str = Field(
        default="", description="What the brief has to settle to answer it"
    )


class LensReading(BaseModel):
    """Everything one adversarial lens found across a chapter's briefs."""

    concerns: list[ProposedConcern] = Field(
        default=[], description="Only faults found; empty means the briefs passed"
    )


class BriefConcern(BaseModel):
    """One persisted adversarial reading of one proposed brief."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part whose brief was read")
    lens: str = Field(description="Which independent question found this")
    finding: str = Field(description="The concrete fault it found")
    settle: str = Field(default="", description="What the lens says would settle it")

    def render(self) -> str:
        """This concern as the repair pass and an operator read it."""
        repair = f" — {self.settle}" if self.settle else ""
        return f"{self.key} [{self.lens}]: {self.finding}{repair}"


class WorkBriefs(BaseModel):
    """Every brief a book planner produced, by the part each is for.

    Stored because the planning and the running are different steps: a pass
    plans from above every outstanding part and then runs them, possibly
    concurrently, possibly over hours. A brief held only in the planner's
    memory would have to be handed down through the loop, which would make the
    loop the thing that knows about briefs.
    """

    briefs: tuple[PartPlan, ...] = Field(
        default=(), description="One entry per part planned"
    )
    crosscut: tuple[CrossCutting, ...] = Field(
        default=(), description="What the whole-book reading found"
    )
    concerns: tuple[BriefConcern, ...] = Field(
        default=(),
        description="What the independent per-brief lenses found before the "
        "stored plans were settled",
    )
    planned_at: str = Field(default="", description="When the planning last ran")

    def brief_for(self, key: str, digest: str = "") -> ComposedBrief | None:
        """The brief planned for one part, where one is still about that part.

        A brief plans a successor to the text the planner read. Once the part
        has been rewritten the text has moved, and the brief is a plan for a
        piece that no longer exists — reusing it on the next pass would rewrite
        the part back toward a draft two revisions old, which is worse than
        having no plan at all. ``digest`` empty asks for whatever is there,
        which is what something reporting on the briefs wants.
        """
        held = next((one for one in self.briefs if one.key == key), None)
        if held is None:
            return None
        if digest and held.digest and held.digest != digest:
            return None
        return held.brief

    def keys(self) -> tuple[str, ...]:
        """Every part a brief was planned for."""
        return tuple(one.key for one in self.briefs)


def outstanding_block(
    manuscript: Manuscript, verdicts: Iterable[NodeVerdict], found: WorkFindings
) -> str:
    """The outstanding parts as the whole-book reading is shown them.

    Why each is outstanding and what the research holds on it, and nothing
    else. This reading is looking for what parts are about to do to each other,
    and what each part currently *says* is the wrong evidence for that: two
    parts that will collide over a paper published last week collide over the
    paper, which is in the findings, not in prose that predates it.
    """

    def lines() -> Iterator[str]:
        """Each outstanding part, named with its reasons and its research."""
        for verdict in verdicts:
            node = manuscript.node(verdict.key)
            if node is None:
                continue
            because = "; ".join(verdict.reasons) or verdict.staleness
            research = briefing(found, verdict.key)
            yield f"### {verdict.key} — {node.title}\n\nOutstanding: {because}\n"
            if research:
                yield research

    return "\n".join(lines())


async def crosscut(
    manuscript: Manuscript, verdicts: Iterable[NodeVerdict], found: WorkFindings
) -> CrossCut:
    """Read the whole book once, for what no reader of one part can see.

    A pass of one part reads nothing here: two parts are needed before they can
    do anything to each other, and spending a book read to establish that one
    part does not collide with itself is the cost that makes a book-level
    planner not worth having.
    """
    held = tuple(verdicts)
    if len(held) < 2:
        return CrossCut()

    read = await query(
        f"{outlined(manuscript)}\n\n## What is outstanding\n\n"
        f"{outstanding_block(manuscript, held, found)}",
        output_type=CrossCut,
        model=stage_model(BRIEF_STAGE),
        system_prompt=CROSSCUT_SYSTEM,
        autonomy="unattended",
        prefix="crosscut",
    )
    if read is None:
        logger.warning(
            "The whole-book reading returned nothing for %s", manuscript.title
        )
        return CrossCut()
    logger.info(
        "Read %s across %d outstanding part(s): %d cross-cutting finding(s)",
        manuscript.title,
        len(held),
        len(read.findings),
    )
    return read


def chapter_block(
    manuscript: Manuscript,
    nodes: Iterable[ManuscriptNode],
    found: WorkFindings,
    verdicts: Iterable[NodeVerdict] = (),
) -> str:
    """The parts of one chapter as their planning is shown them."""

    by_key = {one.key: one for one in verdicts}

    def lines() -> Iterator[str]:
        """Each part with what only the work knows about it."""
        for node in nodes:
            text = held_text_for(manuscript, node)
            figures = inventory_of(text).figures
            budget = budget_for(manuscript, node)
            yield f"### {node.key} — {node.title}\n"
            verdict = by_key[node.key] if node.key in by_key else None
            if verdict is not None:
                because = "; ".join(verdict.reasons) or verdict.staleness
                yield f"The build ledger says it is outstanding because: {because}"
            if budget.render():
                yield budget.render()
            if figures:
                yield "Figures it carries, numbered by the book: " + ", ".join(
                    f"{one.render()} ({one.image})" for one in figures
                )
            research = briefing(found, node.key)
            if research:
                yield research
            yield f"The part as the book holds it now:\n\n{text}\n"

    return "\n\n".join(lines())


def neighbour_block(manuscript: Manuscript, nodes: Iterable[ManuscriptNode]) -> str:
    """The text immediately around these parts, read once for the boundary lens.

    The ordinary planner reads the whole outstanding chapter, which omits a
    settled neighbour at exactly the boundary an outstanding part can cross.
    This adds the predecessor and successor of every planned part, each once,
    without turning a per-chapter review into another whole-book read.
    """
    order = [one for one in manuscript.leaves() if one.path]
    positions = {one.key: at for at, one in enumerate(order)}
    planned = {one.key for one in nodes}

    def neighbours() -> Iterator[ManuscriptNode]:
        """Every settled part immediately beside something being planned."""
        for key in planned:
            if key not in positions:
                continue
            at = positions[key]
            for other in order[max(0, at - 1) : at] + order[at + 1 : at + 2]:
                if other.key not in planned:
                    yield other

    nearby = {one.key: one for one in neighbours()}
    if not nearby:
        return ""
    return "## Immediate neighbours\n\n" + "\n\n".join(
        f"### {one.key} — {one.title}\n\n{held_text_for(manuscript, one)}"
        for one in order
        if one.key in nearby
    )


def plans_block(plans: Iterable[PartPlan]) -> str:
    """Proposed briefs in a stable, labelled form a lens can read."""
    return "## Proposed briefs\n\n" + "\n\n".join(
        f"### {one.key}\n\n```json\n{one.brief.model_dump_json(indent=2)}\n```"
        for one in plans
    )


def review_task(
    manuscript: Manuscript,
    nodes: tuple[ManuscriptNode, ...],
    verdicts: tuple[NodeVerdict, ...],
    found: WorkFindings,
    cut: CrossCut,
    plans: tuple[PartPlan, ...],
) -> str:
    """Everything the independent per-brief readers need, and no whole book."""
    keys = tuple(one.key for one in nodes)
    return "\n\n".join(
        held
        for held in (
            f"# Reviewing plans for parts of {manuscript.title}",
            cut.render(keys),
            chapter_block(manuscript, nodes, found, verdicts),
            neighbour_block(manuscript, nodes),
            plans_block(plans),
        )
        if held
    )


async def review_chapter(
    manuscript: Manuscript,
    nodes: tuple[ManuscriptNode, ...],
    verdicts: tuple[NodeVerdict, ...],
    found: WorkFindings,
    cut: CrossCut,
    plans: tuple[PartPlan, ...],
) -> tuple[BriefConcern, ...]:
    """Read every proposed brief through each adversarial lens independently.

    The four turns run together: their independence is the useful property,
    not making a chapter wait four model latencies. A failed or empty lens does
    not erase the plan or the other readings; it is logged and contributes no
    invented assurance.
    """
    if not plans:
        return ()
    task = review_task(manuscript, nodes, verdicts, found, cut, plans)

    async def read(name: str, system: str) -> tuple[BriefConcern, ...]:
        try:
            answered = await query(
                task,
                output_type=LensReading,
                model=stage_model(BRIEF_STAGE),
                system_prompt=system,
                autonomy="unattended",
                prefix=f"brief-lens-{name}",
            )
        except Exception:
            logger.exception("The %s brief lens failed", name)
            return ()
        if answered is None:
            logger.warning("The %s brief lens returned nothing", name)
            return ()
        keys = {one.key for one in plans}
        return tuple(
            BriefConcern(
                key=one.key,
                lens=name,
                finding=one.finding,
                settle=one.settle,
            )
            for one in answered.concerns
            if one.key in keys
        )

    readings = await asyncio.gather(
        *(read(lens.name, lens.system) for lens in LENS_SYSTEMS)
    )
    concerns = tuple(one for reading in readings for one in reading)
    logger.info(
        "Read %d brief(s) through %d lens(es): %d concern(s)",
        len(plans),
        len(LENS_SYSTEMS),
        len(concerns),
    )
    return concerns


def stamped_plans(
    manuscript: Manuscript,
    nodes: tuple[ManuscriptNode, ...],
    plans: Iterable[PartPlan],
) -> tuple[PartPlan, ...]:
    """Keep only asked-for plans and stamp them with the text they plan."""
    by_key = {one.key: one for one in nodes}

    def kept() -> Iterator[PartPlan]:
        for one in plans:
            if one.key not in by_key:
                logger.warning("A plan names %r, which was not asked about", one.key)
                continue
            node = by_key[one.key]
            yield one.model_copy(
                update={"digest": digest_of(held_text_for(manuscript, node))}
            )

    return tuple(kept())


async def settle_reviews(
    manuscript: Manuscript,
    nodes: tuple[ManuscriptNode, ...],
    verdicts: tuple[NodeVerdict, ...],
    found: WorkFindings,
    cut: CrossCut,
    plans: tuple[PartPlan, ...],
    concerns: tuple[BriefConcern, ...],
) -> tuple[PartPlan, ...]:
    """Answer the independent concerns once, preserving every planned key.

    Nothing is spent where every lens passed. If the repair returns nothing or
    omits a key, the sound initial plan for that key survives instead of a
    transient reader failure turning it into no plan at all.
    """
    if not concerns:
        return plans
    task = (
        f"{review_task(manuscript, nodes, verdicts, found, cut, plans)}\n\n"
        "## Adversarial findings to settle\n\n"
        + "\n".join(f"- {one.render()}" for one in concerns)
    )
    try:
        answered = await query(
            task,
            output_type=ChapterPlans,
            model=stage_model(BRIEF_STAGE),
            system_prompt=LENS_REPAIR_SYSTEM,
            autonomy="unattended",
            prefix="brief-settle",
        )
    except Exception:
        logger.exception("The brief repair failed; keeping proposed plans")
        return plans
    if answered is None:
        logger.warning("The brief repair returned nothing; keeping proposed plans")
        return plans
    repaired = {
        one.key: one for one in stamped_plans(manuscript, nodes, answered.plans)
    }
    return tuple(repaired[one.key] if one.key in repaired else one for one in plans)


def held_text_for(manuscript: Manuscript, node: ManuscriptNode) -> str:
    """One part's text, read for the planner rather than for a run."""
    from pathlib import Path

    from inkwell.manuscript.graph import source_text

    # lup: ignore[dict-str-payload] — a cache keyed by whatever paths a work has
    opened: dict[str, str | None] = {}
    return source_text(Path(manuscript.root), node, opened) or ""


async def plan_chapter(
    manuscript: Manuscript,
    nodes: tuple[ManuscriptNode, ...],
    found: WorkFindings,
    cut: CrossCut,
    verdicts: tuple[NodeVerdict, ...] = (),
) -> tuple[PartPlan, ...]:
    """Plan one chapter's outstanding parts together, against the book reading."""
    if not nodes:
        return ()

    keys = tuple(node.key for node in nodes)
    read = await query(
        "\n\n".join(
            held
            for held in (
                cut.render(keys),
                f"## The parts to plan, of {manuscript.title}\n",
                chapter_block(manuscript, nodes, found, verdicts),
                "Plan each of them.",
            )
            if held
        ),
        output_type=ChapterPlans,
        model=stage_model(BRIEF_STAGE),
        system_prompt=PLANNER_SYSTEM,
        autonomy="unattended",
        prefix="plan-chapter",
    )
    if read is None:
        logger.warning("Planning returned nothing for %s", keys)
        return ()

    return stamped_plans(manuscript, nodes, read.plans)


class ReviewedChapter(BaseModel):
    """One chapter's settled plans and the readings that changed them."""

    model_config = ConfigDict(frozen=True)

    plans: tuple[PartPlan, ...] = ()
    concerns: tuple[BriefConcern, ...] = ()


async def plan_reviewed_chapter(
    manuscript: Manuscript,
    nodes: tuple[ManuscriptNode, ...],
    verdicts: tuple[NodeVerdict, ...],
    found: WorkFindings,
    cut: CrossCut,
) -> ReviewedChapter:
    """Plan a chapter, challenge every brief, and settle what the lenses find."""
    proposed = await plan_chapter(manuscript, nodes, found, cut, verdicts)
    concerns = await review_chapter(manuscript, nodes, verdicts, found, cut, proposed)
    settled = await settle_reviews(
        manuscript, nodes, verdicts, found, cut, proposed, concerns
    )
    return ReviewedChapter(plans=settled, concerns=concerns)


def by_chapter(
    manuscript: Manuscript, verdicts: Iterable[NodeVerdict]
) -> tuple[tuple[ManuscriptNode, ...], ...]:
    """The outstanding parts grouped by the chapter each sits in, in book order.

    A chapter rather than the whole work, because one call emitting two hundred
    briefs is one call to lose, and a chapter is the unit whose parts actually
    share anything. What the chapters cannot see between them is what the
    whole-book reading already carried.
    """
    wanted = tuple(one.key for one in verdicts)
    return tuple(
        held
        for chapter in manuscript.children
        if (held := tuple(node for node in chapter.leaves() if node.key in wanted))
    )


async def plan_work(
    manuscript: Manuscript,
    verdicts: Iterable[NodeVerdict],
    found: WorkFindings | None = None,
) -> WorkBriefs:
    """Plan every outstanding part of a work, from a reader that has seen it all.

    The whole-book reading first, then a chapter at a time against it. What
    comes back is one brief per part, stored rather than handed down, so the
    loop that runs the parts stays a loop that runs parts.
    """
    held = tuple(verdicts)
    research = found if found is not None else WorkFindings()
    cut = await crosscut(manuscript, held, research)

    verdict_of = {one.key: one for one in held}
    chapters = [
        await plan_reviewed_chapter(
            manuscript,
            nodes,
            tuple(verdict_of[node.key] for node in nodes if node.key in verdict_of),
            research,
            cut,
        )
        for nodes in by_chapter(manuscript, held)
    ]
    briefs = tuple(one for chapter in chapters for one in chapter.plans)
    concerns = tuple(concern for chapter in chapters for concern in chapter.concerns)
    logger.info(
        "Planned %d of %d outstanding part(s) of %s",
        len(briefs),
        len(held),
        manuscript.title,
    )
    return WorkBriefs(
        briefs=briefs,
        crosscut=tuple(cut.findings),
        concerns=concerns,
        planned_at=now_stamp(),
    )


def article_plan(
    manuscript: Manuscript, node: ManuscriptNode, brief: ComposedBrief
) -> ArticlePlan:
    """One planned brief as the plan every stage after it reads.

    The same rendering the per-part deriver uses, over the same inputs, which
    is what makes the two producers interchangeable: a run handed a plan cannot
    tell which reader composed it, and nothing downstream should be able to.
    """
    return planned(
        brief,
        manuscript,
        node,
        inventory_of(held_text_for(manuscript, node)).figures,
        budget_for(manuscript, node),
    )
