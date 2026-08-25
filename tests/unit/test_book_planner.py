"""The second producer of a brief: one reader, above every outstanding part.

Both producers emit the same artifact, so nothing downstream knows which one it
got — that is what lets a run testing one subsection derive its own brief and
never open the book, while a full pass plans from a reader that has. What is
worth pinning is what only the book reader can see, that a run prefers a planned
brief over deriving one, and that a brief stops being used once the part it
planned a successor to has been rewritten.
"""

from pathlib import Path

import pytest

from inkwell.manuscript.brief import (
    ComposedBrief,
    CorpusPusher,
    EditorialSection,
    PushedCorpusClaim,
)
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.planner import (
    BriefConcern,
    ChapterPlans,
    CrossCut,
    CrossCutting,
    LensReading,
    PartPlan,
    ProposedConcern,
    WorkBriefs,
    article_plan,
    by_chapter,
    chapter_block,
    chapter_evidence,
    neighbour_block,
    outstanding_block,
    push_for_parts,
    review_chapter,
    settle_reviews,
)
from inkwell.manuscript.state import NodeVerdict, digest_of
from inkwell.manuscript.store import ManuscriptStore

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.1 Bio Risk

Prose about engineered pathogens.

## 2.3.2 Cyber Risk

Prose about cyber risk.
"""

OTHER_PAGES = """\
nav:
- 4.1 - Evaluations: 01.md
title: 04 - Evaluations
"""

EVALUATIONS = """\
# 4.1 Evaluations

## 4.1.1 Cyber Ranges

Prose about ranges.
"""


def two_chapters(root: Path) -> Path:
    """A work with three parts across two chapters."""
    chapters = root / "chapters"
    misuse = chapters / "02"
    misuse.mkdir(parents=True)
    (misuse / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (misuse / "03.md").write_text(MISUSE, encoding="utf-8")
    evaluations = chapters / "04"
    evaluations.mkdir(parents=True)
    (evaluations / ".pages.yml").write_text(OTHER_PAGES, encoding="utf-8")
    (evaluations / "01.md").write_text(EVALUATIONS, encoding="utf-8")
    return chapters


def outstanding(*keys: str) -> tuple[NodeVerdict, ...]:
    """Those parts, out of date because something they lean on moved."""
    return tuple(
        NodeVerdict(key=key, staleness="upstream", reasons=("a paper landed",))
        for key in keys
    )


SETTLED = ComposedBrief(
    thesis="Cyber offence is being measured.",
    opening=EditorialSection(
        title="What is measured",
        establishes="The measurement.",
        order_reason="It defines the object before interpretation.",
        evidence=["A current evaluation."],
        handoff="The reader can interpret it.",
        key_points=[],
    ),
)


class TestPlanningGoesChapterByChapter:
    """One call emitting two hundred briefs is one call to lose, and a chapter
    is the unit whose parts actually share anything."""

    def test_outstanding_parts_are_grouped_by_their_chapter(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        grouped = by_chapter(work, outstanding("02/03/2.3.2", "04/01/4.1.1"))

        assert [[one.key for one in held] for held in grouped] == [
            ["02/03/2.3.2"],
            ["04/01/4.1.1"],
        ]

    def test_a_chapter_with_nothing_outstanding_is_not_planned(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        grouped = by_chapter(work, outstanding("02/03/2.3.2"))

        assert len(grouped) == 1

    def test_the_groups_come_in_book_order(self, tmp_path: Path) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        grouped = by_chapter(work, outstanding("04/01/4.1.1", "02/03/2.3.1"))

        assert grouped[0][0].key.startswith("02")


class TestWhatTheWholeBookReaderIsShown:
    """Why each part is outstanding and what the research holds on it, and not
    what each part currently says: two parts that will collide over a paper
    published last week collide over the paper, not over prose that predates
    it."""

    def test_each_outstanding_part_is_named_with_its_reasons(
        self, tmp_path: Path
    ) -> None:
        from inkwell.manuscript.findings import WorkFindings

        work = read_manuscript(two_chapters(tmp_path))

        said = outstanding_block(work, outstanding("02/03/2.3.2"), WorkFindings())

        assert "02/03/2.3.2" in said
        assert "a paper landed" in said

    def test_a_key_the_work_does_not_hold_contributes_nothing(
        self, tmp_path: Path
    ) -> None:
        from inkwell.manuscript.findings import WorkFindings

        work = read_manuscript(two_chapters(tmp_path))

        assert outstanding_block(work, outstanding("09/09/9.9.9"), WorkFindings()) == ""


class TestTheLedgerReachesThePlanner:
    """The planner cannot respect the scope of a rewrite it was never shown."""

    def test_the_reason_a_part_is_outstanding_sits_beside_its_evidence(
        self, tmp_path: Path
    ) -> None:
        from inkwell.manuscript.findings import WorkFindings

        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        said = chapter_block(
            work,
            (node,),
            WorkFindings(),
            outstanding("02/03/2.3.2"),
        )

        assert "a paper landed" in said
        assert "Prose about cyber risk" not in said

    def test_pushed_evidence_arrives_before_placement_without_standing_prose(
        self, tmp_path: Path
    ) -> None:
        from inkwell.manuscript.findings import Bearing, WorkFindings

        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        found = WorkFindings(
            bearings=(
                Bearing(
                    key=node.key,
                    document="current",
                    claim="An urgent agentic campaign crossed the full attack chain.",
                    cited="The current system card",
                    why="It changes what cyber risk opens with.",
                ),
            )
        )

        said = chapter_block(work, (node,), found, outstanding(node.key))

        assert said.index("urgent agentic campaign") < said.index("Editorial placement")
        assert "Prose about cyber risk" not in said

    @pytest.mark.asyncio
    async def test_both_brief_producers_digest_the_same_corpus_packet(
        self, tmp_path: Path
    ) -> None:
        from inkwell.manuscript.findings import WorkFindings

        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        class PushesOne(CorpusPusher):
            async def push(
                self, topic: str, subject: str = ""
            ) -> tuple[PushedCorpusClaim, ...]:
                return (
                    PushedCorpusClaim(
                        title="A current system card",
                        claim="A current campaign crossed the attack chain.",
                        source="lab/card",
                    ),
                )

        pushed = await push_for_parts(work, (node,), PushesOne())
        full = chapter_evidence(
            work, (node,), WorkFindings(), outstanding(node.key), pushed
        )[0]
        direct = chapter_evidence(work, (node,), WorkFindings(), outstanding(node.key))[
            0
        ].model_copy(update={"corpus": pushed[0].claims})

        assert full.digest() == direct.digest()


class TestTheBoundaryLensCanReadSettledNeighbours:
    """Boundaries need durable titles, not prose from an earlier instantiation."""

    def test_the_parts_immediately_before_and_after_are_shown(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        said = neighbour_block(work, (node,))

        assert "02/03/2.3.1" in said
        assert "04/01/4.1.1" in said
        assert "engineered pathogens" not in said
        assert "Prose about ranges" not in said


class TestACrossCuttingFindingReachesTheChapterItIsAbout:
    """A chapter shown every finding about the book would be shown mostly
    findings about parts it is not planning."""

    def held(self) -> CrossCut:
        return CrossCut(
            findings=[
                CrossCutting(
                    parts=["02/03/2.3.2", "04/01/4.1.1"],
                    finding="both are about to introduce the same paper",
                    settle="2.3.2 introduces it; 4.1.1 refers to it",
                ),
                CrossCutting(
                    parts=["02/03/2.3.1"], finding="something about bio risk only"
                ),
            ]
        )

    def test_a_chapter_sees_what_names_its_parts(self) -> None:
        said = self.held().render(("02/03/2.3.2",))

        assert "the same paper" in said
        assert "bio risk only" not in said

    def test_a_chapter_naming_none_of_them_is_shown_nothing(self) -> None:
        assert self.held().render(("09/09/9.9.9",)) == ""

    def test_everything_about_one_part_is_findable(self) -> None:
        assert len(self.held().touching("02/03/2.3.2")) == 1

    def test_the_settlement_travels_with_the_finding(self) -> None:
        assert "4.1.1 refers to it" in self.held().findings[0].render()


class TestABriefStopsBeingUsedOnceItsPartMoves:
    """A brief plans a successor to the text the planner read. Reused after the
    part is rewritten, it would pull the part back toward a draft two revisions
    old — worse than having no plan at all."""

    def held(self) -> WorkBriefs:
        return WorkBriefs(
            briefs=(
                PartPlan(
                    key="02/03/2.3.2", brief=SETTLED, digest=digest_of("as planned")
                ),
            )
        )

    def test_a_brief_for_the_text_it_planned_is_used(self) -> None:
        found = self.held().brief_for("02/03/2.3.2", digest_of("as planned"))

        assert found is not None

    def test_a_brief_for_text_that_has_moved_is_not(self) -> None:
        assert self.held().brief_for("02/03/2.3.2", digest_of("rewritten")) is None

    def test_asking_without_a_digest_gets_whatever_is_there(self) -> None:
        """What something reporting on the briefs wants, as against what a run
        working to one does."""
        assert self.held().brief_for("02/03/2.3.2") is not None

    def test_a_part_nothing_planned_has_none(self) -> None:
        assert self.held().brief_for("02/03/2.3.1", digest_of("x")) is None


class TestBothProducersEmitTheSameThing:
    """A run handed a plan cannot tell which reader composed it, and nothing
    downstream should be able to."""

    def test_a_planned_brief_becomes_the_plan_every_stage_reads(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        held = article_plan(work, node, SETTLED)

        assert held.title == node.title
        assert held.thesis == SETTLED.thesis
        assert held.target_format == work.target_format

    def test_it_carries_the_contract_a_derived_one_carries(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        held = article_plan(work, node, SETTLED)

        assert any("opening with its own heading" in one for one in held.deliverables)
        assert not any("words" in one for one in held.deliverables)
        assert any("words" in one for one in held.constraints)

    def test_the_explicit_editorial_opening_leads_over_stale_order(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        brief = ComposedBrief(
            thesis="Agentic intrusion changes the threat model.",
            opening=EditorialSection(
                title="The live agentic campaign",
                establishes="The urgent evidence.",
                order_reason="It is the pressing current event.",
                evidence=["The current incident record."],
                handoff="The older outage becomes context.",
                key_points=[],
            ),
            sections=[
                EditorialSection(
                    title="The older outage",
                    establishes="Background.",
                    order_reason="It historicizes the current event.",
                    evidence=["The incident postmortem."],
                    handoff="The comparison is complete.",
                    key_points=[],
                )
            ],
        )

        held = article_plan(work, node, brief)

        assert [one.title for one in held.sections] == [
            "The live agentic campaign",
            "The older outage",
        ]


class TestWhatWasPlannedSurvivesThePlanner:
    """Planning and running are different steps, hours apart."""

    def test_a_work_nothing_planned_holds_nothing(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")

        assert store.load_briefs("atlas").briefs == ()

    def test_what_was_planned_is_read_back(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_briefs(
            "atlas",
            WorkBriefs(briefs=(PartPlan(key="02/03/2.3.2", brief=SETTLED),)),
        )

        assert store.load_briefs("atlas").keys() == ("02/03/2.3.2",)

    def test_the_cross_cutting_reading_is_kept_with_them(self, tmp_path: Path) -> None:
        """It is what a person reads to understand why the plans say what they
        say, and it exists nowhere else."""
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_briefs(
            "atlas",
            WorkBriefs(crosscut=(CrossCutting(parts=["a"], finding="something"),)),
        )

        assert store.load_briefs("atlas").crosscut[0].finding == "something"

    def test_the_adversarial_readings_are_kept_with_the_settled_briefs(
        self, tmp_path: Path
    ) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_briefs(
            "atlas",
            WorkBriefs(
                concerns=(
                    BriefConcern(
                        key="02/03/2.3.2",
                        lens="ledger",
                        finding="the plan exceeds what changed",
                    ),
                )
            ),
        )

        assert store.load_briefs("atlas").concerns[0].lens == "ledger"


@pytest.mark.asyncio
async def test_every_brief_receives_one_structured_editorial_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inkwell.manuscript import planner
    from inkwell.manuscript.findings import WorkFindings

    work = read_manuscript(two_chapters(tmp_path))
    node = work.node("02/03/2.3.2")
    assert node is not None
    prefixes: list[str] = []

    async def read(*args: object, **kwargs: object) -> LensReading:
        prefix = str(kwargs["prefix"])
        assert "max_thinking_tokens" not in kwargs
        prefixes.append(prefix)
        return LensReading(
            concerns=[
                ProposedConcern(key="02/03/2.3.2", finding=f"found by {prefix}"),
                ProposedConcern(key="invented", finding="belongs nowhere"),
            ]
        )

    monkeypatch.setattr(planner, "query", read)
    concerns = await review_chapter(
        work,
        (node,),
        outstanding("02/03/2.3.2"),
        WorkFindings(),
        CrossCut(),
        (PartPlan(key=node.key, brief=SETTLED),),
    )

    assert prefixes == ["brief-audit"]
    assert {one.lens for one in concerns} == {"editorial"}
    assert {one.key for one in concerns} == {"02/03/2.3.2"}


@pytest.mark.asyncio
async def test_a_repair_that_omits_a_plan_does_not_erase_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inkwell.manuscript import planner
    from inkwell.manuscript.findings import WorkFindings

    work = read_manuscript(two_chapters(tmp_path))
    nodes = tuple(
        node
        for key in ("02/03/2.3.1", "02/03/2.3.2")
        if (node := work.node(key)) is not None
    )
    proposed = tuple(
        PartPlan(key=node.key, brief=SETTLED, digest="planned") for node in nodes
    )
    repaired = SETTLED.model_copy(update={"thesis": "The reviewed thesis."})

    async def repair(*args: object, **kwargs: object) -> ChapterPlans:
        return ChapterPlans(plans=[PartPlan(key="02/03/2.3.2", brief=repaired)])

    monkeypatch.setattr(planner, "query", repair)
    settled = await settle_reviews(
        work,
        nodes,
        outstanding(*(one.key for one in nodes)),
        WorkFindings(),
        CrossCut(),
        proposed,
        (
            BriefConcern(
                key="02/03/2.3.2",
                lens="ledger",
                finding="scope drift",
            ),
        ),
    )

    assert settled[0].brief == SETTLED
    assert settled[1].brief.thesis == "The reviewed thesis."
    assert all(one.digest for one in settled)


@pytest.mark.asyncio
async def test_one_outstanding_part_costs_no_book_read(tmp_path: Path) -> None:
    """Two parts are needed before they can do anything to each other, and
    spending a book read to establish that one part does not collide with
    itself is what would make this not worth having."""
    from inkwell.manuscript.findings import WorkFindings
    from inkwell.manuscript.planner import crosscut

    work = read_manuscript(two_chapters(tmp_path))

    assert (
        await crosscut(work, outstanding("02/03/2.3.2"), WorkFindings()) == CrossCut()
    )
