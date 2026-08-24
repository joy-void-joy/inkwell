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

**It never opens the prose.** The deriver receives declared tree titles, the
titles immediately beside this part, its work-owned constraints, and research
pushed specifically for that durable placement. The standing passage and its
neighbours' text reach the writer and inheritance pass later; they cannot shape
editorial intent. The other producer reads the whole declared outline and
emits every part's brief at once, which lets it see cross-cutting staleness
without reading the current book instantiation. Both emit an ``ArticlePlan``.

**The plan stage is not skipped; it is answered.** A run handed a brief records
it and moves on — the Plan tab is still written, the section tabs still created,
the overview still updated. Skipping the stage would have saved the same money
and silently taken all of that with it.
"""

import logging
from hashlib import sha256
from abc import ABC, abstractmethod
from asyncio import gather
from collections.abc import Iterator
from itertools import zip_longest
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from inkwell.agent.client import query
from inkwell.agent.config import stage_model
from inkwell.agent.tool_policy import planning_tool_names
from inkwell.agent.models import (
    ArticlePlan,
    ResearchQuestion,
    SectionPlan,
    SourceQuote,
    WordBudget,
)
from inkwell.manuscript.budget import LengthBudget
from inkwell.manuscript.findings import Bearing, WorkFindings
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
cheaper work — the saving is that it reads a typed evidence packet rather than
standing prose and a research compilation, not that it thinks less.
"""

BRIEF_SYSTEM = """\
You are choosing the editorial intent for one part of a book.

Your task contains pushed evidence, the part's place and immediate boundaries, \
the reason it is running, its length budget, and figures that inheritance will \
protect. It deliberately does not contain the standing prose. Choose what a \
reader needs first from this evidence, then build the successor around it. The \
opening is a separate field because choosing it is an editorial decision, not \
the first surviving heading of an older draft.

For every section, state what it establishes, why it belongs at that point, \
which pushed findings it uses, and what it hands to the following section. \
Those fields become the writer's section brief; do not leave sequencing logic \
implicit in a heading.

Treat findings as established with exactly their stated caveats. Ask research \
questions only for gaps the pushed evidence does not settle. Fit the complete \
argument to the stated budget, respect its place between its neighbours, and \
leave preservation of standing claims, citations, figures, and voice to the \
inheritance pass after drafting.

The pushed listing is one page of a larger corpus, and it says how many \
documents matched. Where it reads thin on the part's actual subject, or \
where what it carries is adjacent rather than central, use corpus_search to \
ask for yourself — page past the listing with offset, narrow by tag or by \
date, and read a document whole where the summary will not settle it. A \
subject the first page happened to miss is not a subject the corpus lacks, \
and this is the last stage in a position to notice the difference: every \
stage after this one plans inside the structure you choose here.

Be specific and brief. Give every section one or two checkable key points.
"""


class PushedCorpusClaim(BaseModel):
    """One judged corpus claim retrieved before subsection planning starts."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(description="The matched document's title")
    claim: str = Field(description="What judging or a cached full read establishes")
    source: str = Field(description="The corpus source and document identifier")
    depth: Literal["judged", "distilled"] = Field(
        default="judged", description="Whether this came from judging or a full read"
    )
    caveat: str = Field(default="", description="Conditions the document attaches")
    locator: str = Field(default="", description="Where the full read found it")
    published: str = Field(default="", description="Source-stated publication date")
    url: str = Field(default="", description="Where the document was published")

    def render(self) -> str:
        """This claim as the evidence-first planning packet carries it."""
        dated = f" ({self.published})" if self.published else ""
        caveat = f"\n  caveat: {self.caveat}" if self.caveat else ""
        located = f" [{self.locator}]" if self.locator else ""
        return (
            f"- **{self.title}**{dated} [{self.depth}] — {self.claim}{located}"
            f"{caveat}\n  {self.source}"
        )


class CorpusPusher(ABC):
    """Who retrieves corpus claims before a subsection chooses its intent.

    ``subject`` is the part as it stands, and it never reaches the deriver —
    it is a query, not a plan. Retrieval is the one place the old prose is
    worth reading, because it is the only account of what the part is about;
    everything downstream still sees evidence and no old arrangement.
    """

    @abstractmethod
    async def push(
        self, topic: str, subject: str = ""
    ) -> tuple[PushedCorpusClaim, ...]:
        """Return judged claims nearest a subsection's place and its subject."""


class LocalCorpusPusher(CorpusPusher):
    """The ordinary plan stage's narrow retrieval, asked both ways at once.

    Asked only where the part sits, the corpus answers with documents about
    that: a query reading "Risks > Misuse Risks > Cyber Risk" is nearest to
    papers on risk taxonomy and assurance frameworks, and a subsection on
    cyber attacks is briefed on three-lines-of-defence and tort law. The
    part's own text is the only statement of its subject anything here holds,
    so it asks a second time on that and merges the two rankings.
    """

    async def push(
        self, topic: str, subject: str = ""
    ) -> tuple[PushedCorpusClaim, ...]:
        from inkwell.agent.config import corpus_root, corpus_semantics
        from inkwell.agent.pipeline import CORPUS_BRIEFING_LIMIT, briefing_claim
        from inkwell.corpus.distillation import read_distillation
        from inkwell.corpus.retrieval import (
            CorpusHit,
            CorpusQuery,
            read_index,
            search_corpus,
        )
        from inkwell.corpus.storage import CorpusStore

        store = CorpusStore(root=corpus_root())
        semantics = corpus_semantics(store)

        async def nearest(question: str) -> tuple[CorpusHit, ...]:
            """What the corpus places nearest one way of asking."""
            answer = await search_corpus(
                CorpusQuery(tier="narrow", like=question, limit=CORPUS_BRIEFING_LIMIT),
                store,
                semantics=semantics,
            )
            return answer.documents

        def both_ways(
            placed: tuple[CorpusHit, ...], about: tuple[CorpusHit, ...]
        ) -> tuple[CorpusHit, ...]:
            """Both rankings a turn at a time, each document only once.

            Alternated rather than concatenated: a briefing that runs one
            ranking out before starting the other is the first ranking, with
            the second appended past the limit where nothing reads it.
            """
            turns = (hit for pair in zip_longest(placed, about) for hit in pair if hit)
            return tuple({f"{one.source}/{one.slug}": one for one in turns}.values())

        ranked = await gather(nearest(topic), nearest(subject or topic))
        # lup: ignore[silent-truncation] — CORPUS_BRIEFING_LIMIT is the
        # briefing's declared size, which the two ways of asking now share
        documents = both_ways(*ranked)[:CORPUS_BRIEFING_LIMIT]
        entries = read_index(store).entries

        def claims() -> Iterator[PushedCorpusClaim]:
            """Judged summaries, plus cached full reads of the top matches."""
            for at, hit in enumerate(documents):
                source = f"{hit.source}/{hit.slug}"
                yield PushedCorpusClaim(
                    title=hit.title,
                    claim=briefing_claim(hit),
                    source=source,
                    published=hit.published,
                    url=hit.url,
                )
                if at >= SUBSECTION_DISTILLED_DOCUMENTS:
                    continue
                entry = next(
                    (
                        one
                        for one in entries
                        if one.source == hit.source and one.document.slug == hit.slug
                    ),
                    None,
                )
                if entry is None or not entry.document.content_sha256:
                    continue
                distilled = read_distillation(store, entry.document.content_sha256)
                if distilled is None or not distilled.read():
                    continue
                for finding in distilled.findings:
                    yield PushedCorpusClaim(
                        title=distilled.title or hit.title,
                        claim=finding.claim,
                        source=source,
                        depth="distilled",
                        caveat=finding.caveat,
                        locator=finding.locator,
                        published=distilled.published or hit.published,
                        url=distilled.url or hit.url,
                    )

        return tuple(claims())


SUBSECTION_DISTILLED_DOCUMENTS = 5
"""Top corpus matches whose cached full-document findings reach planning."""


def subsection_topic(manuscript: Manuscript, node: ManuscriptNode) -> str:
    """A corpus query made only of the work's durable declared structure."""
    trail = tuple(
        one.title for one in manuscript.walk() if node.key.startswith(f"{one.key}/")
    )
    return (
        f"Part of {manuscript.title}: "
        f"{' > '.join((*trail, node.title))}. {manuscript.target_format}."
    )


class EditorialEvidence(BaseModel):
    """What can decide a part's successor without inheriting its old shape.

    Standing prose is deliberately absent. Its material is protected by the
    inheritance pass; showing its headings here would make the old arrangement
    the cheapest available plan.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part, by the key the work uses")
    title: str = Field(description="The part's standing title")
    work: str = Field(description="The work this part belongs to")
    placement: tuple[str, ...] = Field(
        default=(), description="Ancestor titles, from the work to the parent"
    )
    neighbours: tuple[str, ...] = Field(
        default=(), description="Immediately adjacent parts, by key and title"
    )
    reasons: tuple[str, ...] = Field(
        default=(), description="What made this part eligible to run"
    )
    corpus: tuple[PushedCorpusClaim, ...] = Field(
        default=(), description="Subsection-scoped claims pushed before planning"
    )
    findings: tuple[Bearing, ...] = Field(
        default=(), description="Full-document findings placed on this part"
    )
    figures: tuple[Figure, ...] = Field(
        default=(), description="Figures inheritance must preserve"
    )
    budget: LengthBudget = Field(
        default_factory=LengthBudget, description="This part's share of its chapter"
    )

    def digest(self) -> str:
        """Fingerprint the evidence a stored plan claims to have considered."""
        return sha256(self.model_dump_json().encode()).hexdigest()

    def render(self) -> str:
        """Render pushed evidence before placement and editorial constraints."""
        pushed = "\n".join(one.render() for one in self.corpus)
        research = "\n".join(
            f"- {one.render()}"
            + (f"\n  why here: {one.why}" if one.why else "")
            + (f'\n  "{one.quote}"' if one.quote else "")
            + (f" [{one.locator}]" if one.locator else "")
            for one in self.findings
        )
        changed = "\n".join(f"- {one}" for one in self.reasons)
        nearby = "\n".join(f"- {one}" for one in self.neighbours)
        figures = "\n".join(f"- {one.render()} — {one.image}" for one in self.figures)
        blocks = (
            "## What the corpus pushes to this subsection\n\n"
            + (pushed or "No corpus document matched this subsection."),
            "## Evidence pushed to this part\n\n"
            + (research or "No corpus finding is currently placed on this part."),
            f"## Editorial placement\n\n{self.key} — {self.title}\n"
            f"{' > '.join((*self.placement, self.title))} — of {self.work}",
            f"## Why it is running\n\n{changed or 'It was selected directly.'}",
            f"## Immediate boundaries\n\n{nearby}" if nearby else "",
            self.budget.render(),
            f"## Figures to preserve later\n\n{figures}" if figures else "",
        )
        return "\n\n".join(one for one in blocks if one)


def evidence_for(
    manuscript: Manuscript,
    node: ManuscriptNode,
    found: WorkFindings,
    *,
    reasons: tuple[str, ...] = (),
    corpus: tuple[PushedCorpusClaim, ...] = (),
    figures: tuple[Figure, ...] = (),
    budget: LengthBudget | None = None,
) -> EditorialEvidence:
    """Compile one part's pushed evidence and place, without opening its prose."""
    ancestors = tuple(
        one.title for one in manuscript.walk() if node.key.startswith(f"{one.key}/")
    )
    leaves = tuple(one for one in manuscript.leaves() if one.path)
    keys = tuple(one.key for one in leaves)
    nearby: tuple[ManuscriptNode, ...] = ()
    if node.key in keys:
        at = keys.index(node.key)
        nearby = (*leaves[max(0, at - 1) : at], *leaves[at + 1 : at + 2])
    return EditorialEvidence(
        key=node.key,
        title=node.title,
        work=manuscript.title,
        placement=ancestors,
        neighbours=tuple(f"{one.key} — {one.title}" for one in nearby),
        reasons=reasons,
        corpus=corpus,
        findings=found.on(node.key),
        figures=figures,
        budget=budget or LengthBudget(),
    )


class LegacyBrief(TypedDict, total=False):
    """The two fields needed to upgrade a stored pre-intent brief."""

    opening: JsonValue
    sections: list[JsonValue]


class LegacyEditorialSection(TypedDict, total=False):
    """Fields accepted while upgrading an older generic section plan."""

    title: str
    summary: str
    purpose: str
    establishes: str
    order_reason: str
    evidence: list[str]
    handoff: str
    key_points: list[str]
    quotes_to_include: list[str]


class EditorialSection(BaseModel):
    """One section's purpose before it is compiled for a generic writer."""

    title: str = Field(description="The section heading")
    establishes: str = Field(description="What this section makes true for the reader")
    order_reason: str = Field(
        description="Why it belongs at this point in the argument"
    )
    evidence: list[str] = Field(
        description="Claims or cited findings from the evidence packet it uses"
    )
    handoff: str = Field(description="What the next section can assume after this one")
    key_points: list[str] = Field(description="Specific points the writer must make")
    quotes_to_include: list[str] = Field(
        default_factory=list, description="References to preserved source quotes"
    )

    @model_validator(mode="before")
    @classmethod
    def upgrade_generic_section(
        cls, value: LegacyEditorialSection
    ) -> LegacyEditorialSection:
        """Keep persisted generic plans readable after intent became explicit."""
        missing = ["Legacy plan carried no evidence reference."]
        if "establishes" in value:
            return value
        if "summary" not in value:
            raise ValueError("A legacy section needs its summary")
        migrated = value.copy()
        migrated["establishes"] = (
            value["purpose"]
            if "purpose" in value and value["purpose"]
            else value["summary"]
        )
        migrated["order_reason"] = "It follows the plan's declared reading order."
        migrated["evidence"] = (
            value["evidence"] if "evidence" in value and value["evidence"] else missing
        )
        migrated["handoff"] = (
            value["handoff"]
            if "handoff" in value and value["handoff"]
            else "The following section continues the argument."
        )
        return migrated

    def writer_plan(self) -> SectionPlan:
        """Compile editorial purpose into fields every section writer reads."""
        return SectionPlan(
            title=self.title,
            summary=self.establishes,
            key_points=self.key_points,
            purpose=f"{self.establishes} Why here: {self.order_reason}",
            evidence=self.evidence,
            handoff=self.handoff,
            quotes_to_include=self.quotes_to_include,
        )


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
    opening: EditorialSection = Field(
        description="The opening chosen from pushed evidence, not the old order"
    )
    sections: list[EditorialSection] = Field(
        default=[], description="The successor's remaining sections, in reading order"
    )
    research_questions: list[ResearchQuestion] = Field(
        default=[],
        description="What has to be settled before this can be written — about "
        "the subject, never about what the current text happens to say",
    )
    source_quotes: list[SourceQuote] = Field(
        default=[],
        description="Verbatim passages carried by pushed evidence worth using",
    )
    direction: str = Field(
        default="",
        description="What the writer most needs to know about this part that "
        "the sections do not say — including what must give to meet the budget",
    )
    constraints: list[str] = Field(
        default=[],
        description="Short checkable items the successor must satisfy",
    )

    @model_validator(mode="before")
    @classmethod
    def promote_legacy_opening(cls, value: LegacyBrief) -> LegacyBrief:
        """Read briefs stored before the opening became an explicit decision."""
        if "opening" in value or "sections" not in value or not value["sections"]:
            return value
        migrated = value.copy()
        migrated["opening"] = value["sections"][0]
        migrated["sections"] = value["sections"][1:]
        return migrated


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
        from inkwell.agent.pipeline import build_research_servers

        return await query(
            task,
            output_type=ComposedBrief,
            model=self.model or stage_model(BRIEF_STAGE),
            system_prompt=BRIEF_SYSTEM,
            tools=["Read", "Glob", "Grep"],
            mcp_servers=build_research_servers(),
            allowed_tools=planning_tool_names(),
            cwd=root,
            autonomy="unattended",
            prefix="brief",
        )


def asked(evidence: EditorialEvidence) -> str:
    """What the deriver sees, with pushed findings first and no standing prose."""
    return (
        f"# Planning {evidence.key} — {evidence.title}\n\n"
        f"{evidence.render()}\n\nChoose the editorial intent and plan the successor."
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
            yield (f"a piece between {budget.floor():,} and {budget.ceiling():,} words")

    return ArticlePlan(
        title=node.title,
        thesis=composed.thesis,
        target_format=manuscript.target_format,
        sections=[
            composed.opening.writer_plan(),
            *(one.writer_plan() for one in composed.sections),
        ],
        research_questions=composed.research_questions,
        source_quotes=composed.source_quotes,
        author_direction=composed.direction,
        constraints=composed.constraints,
        deliverables=list(owed()),
        conventions=[],
        voice_notes="",
        word_budget=(
            WordBudget(minimum=budget.floor(), maximum=budget.ceiling())
            if budget.holds
            else None
        ),
    )


async def compose(
    manuscript: Manuscript,
    node: ManuscriptNode,
    evidence: EditorialEvidence,
    room: Path,
    *,
    writer: BriefWriter | None = None,
) -> ArticlePlan | None:
    """Plan one part from its place in the work, and keep the plan.

    ``None`` where the deriver came back with nothing. Non-authoritative
    manuscript material cannot self-plan, so the pipeline refuses before
    spending on research or drafting rather than inheriting the old shape.
    """
    composed = await (writer if writer is not None else ModelBrief()).compose(
        asked(evidence),
        Path(manuscript.root),
    )
    if composed is None:
        logger.warning("No prose-blind brief was composed for %s", node.key)
        return None

    held = planned(composed, manuscript, node, evidence.figures, evidence.budget)
    (room / BRIEF_FILE).write_text(held.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "Brief for %s: %d section(s), %d research question(s)",
        node.key,
        len(held.sections),
        len(held.research_questions),
    )
    return held
