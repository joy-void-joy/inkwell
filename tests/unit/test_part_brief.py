"""The plan a part run is handed, instead of the one it would derive.

A run holding one subsection planned that subsection's existing sections,
because that is all it could see. What is worth pinning is that the plan now
arrives from outside carrying what only the work knows — the budget, the
figures, what the run owes — that the plan stage *records* it rather than being
skipped over, and that a deriver which comes back with nothing leaves the run
stopped before non-authoritative prose can become its fallback plan.
"""

from pathlib import Path

import pytest

from inkwell.agent.models import ArticlePlan
from inkwell.agent.pipeline import PipelineRunner
from inkwell.manuscript.brief import (
    BRIEF_FILE,
    BriefWriter,
    ComposedBrief,
    EditorialSection,
    LocalCorpusPusher,
    asked,
    compose,
    evidence_for,
    planned,
)
from inkwell.manuscript.budget import LengthBudget
from inkwell.manuscript.findings import Bearing, WorkFindings
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.inventory import Figure
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.corpus.distillation import Distillation, Finding, write_distillation
from inkwell.corpus.semantics import SemanticLayer
from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument
from inkwell.corpus.tags import DocumentTags

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.2 Cyber Risk

Prose about cyber risk.
"""


def atlas_like(root: Path) -> Path:
    """A one-part work, small enough to read at once."""
    chapter = root / "chapters" / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    return root / "chapters"


def part_of(tmp_path: Path) -> tuple[Manuscript, ManuscriptNode]:
    """The work and the one part these tests plan."""
    work = read_manuscript(atlas_like(tmp_path))
    node = work.node("02/03/2.3.2")
    assert node is not None
    return work, node


class Answers(BriefWriter):
    """A deriver that answers with what it was constructed with."""

    def __init__(self, brief: ComposedBrief | None) -> None:
        self.brief = brief
        self.asked: list[str] = []

    async def compose(self, task: str, root: Path) -> ComposedBrief | None:
        self.asked.append(task)
        return self.brief


SETTLED = ComposedBrief(
    thesis="Cyber offence is being measured, and the measurements are moving.",
    opening=EditorialSection(
        title="What is measured",
        establishes="The evaluations.",
        order_reason="It defines the measurement before interpreting it.",
        evidence=["A current evaluation."],
        handoff="The reader can interpret the trend.",
        key_points=[],
    ),
    direction="Lead with the sandbox escape.",
)

FIGURE = Figure(
    number="Figure 2.13",
    image="Images/cJx_Image_13.png",
    caption="Stages of a cyberattack.",
    block="<figure>…</figure>",
)


class TestWhatTheDeriverIsShown:
    """Pushed evidence and durable placement, never the standing instantiation."""

    def test_the_part_is_named_by_the_key_state_uses(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        said = asked(evidence_for(work, node, WorkFindings()))

        assert "02/03/2.3.2" in said

    def test_the_standing_material_is_not_available_to_open(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)

        said = asked(evidence_for(work, node, WorkFindings()))

        assert "/room/part.md" not in said
        assert "Prose about cyber risk" not in said

    def test_the_work_owned_budget_is_what_the_planner_reads(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)

        said = asked(
            evidence_for(work, node, WorkFindings(), budget=LengthBudget(holds=1000))
        )

        assert "1,500 words" in said

    def test_what_the_research_holds_travels_with_it(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        found = WorkFindings(
            bearings=(Bearing(key=node.key, document="d", claim="A current thing."),)
        )
        said = asked(evidence_for(work, node, found))

        assert "A current thing." in said

    @pytest.mark.asyncio
    async def test_cached_full_document_findings_join_the_judged_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = CorpusStore(root=tmp_path / "corpus")
        store.save(
            SourceShard(
                source="mythos",
                documents=[
                    StoredDocument(
                        slug="system-card",
                        url="https://example.test/glasswing",
                        title="Project Glasswing system card",
                        summary="The system evaluates autonomous cyber work.",
                        content_sha256="glasswing-bytes",
                        tags=DocumentTags(judged=True),
                    )
                ],
            )
        )
        write_distillation(
            store,
            Distillation(
                content_sha256="glasswing-bytes",
                source="mythos",
                slug="system-card",
                title="Project Glasswing system card",
                findings=(
                    Finding(
                        claim="A public GitHub incident exposed the live campaign.",
                        caveat="The system card describes one observed campaign.",
                        locator="Incident report",
                    ),
                ),
            ),
        )
        monkeypatch.setattr("inkwell.agent.config.corpus_root", lambda: store.root)
        monkeypatch.setattr(
            "inkwell.agent.config.corpus_semantics",
            lambda held: SemanticLayer(store=held, enabled=False),
        )

        claims = await LocalCorpusPusher().push("Cyber Risk")

        assert {one.depth for one in claims} == {"judged", "distilled"}
        assert any("GitHub incident" in one.claim for one in claims)

    @pytest.mark.asyncio
    async def test_asking_both_ways_does_not_brief_one_document_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The place and the subject reach the same document, which is normal.

        A briefing that listed it once per way of asking would spend its size
        on repeats and read as two independent sources for one claim.
        """
        store = CorpusStore(root=tmp_path / "corpus")
        store.save(
            SourceShard(
                source="mythos",
                documents=[
                    StoredDocument(
                        slug="system-card",
                        url="https://example.test/glasswing",
                        title="Project Glasswing system card",
                        summary="The system evaluates autonomous cyber work.",
                        tags=DocumentTags(judged=True),
                    )
                ],
            )
        )
        monkeypatch.setattr("inkwell.agent.config.corpus_root", lambda: store.root)
        monkeypatch.setattr(
            "inkwell.agent.config.corpus_semantics",
            lambda held: SemanticLayer(store=held, enabled=False),
        )

        claims = await LocalCorpusPusher().push(
            "Risks > Misuse Risks > Cyber Risk", "Prose about autonomous cyber work."
        )

        assert [one.source for one in claims] == ["mythos/system-card"]


class TestWhoWroteTheDirection:
    """A composed brief is a reading of the evidence, not the author's word.

    It is written before research runs, from one page of a corpus listing, so
    a stage that finds evidence against one of its constraints has to be able
    to tell that constraint apart from something a person asked for.
    """

    def test_a_composed_brief_says_a_planner_wrote_its_direction(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget())

        assert held.direction_from == "planner"

    def test_a_plan_carries_the_author_by_default(self) -> None:
        """Nothing composed it, so nothing may overturn it on evidence."""
        assert ArticlePlan.model_fields["direction_from"].default == "author"


class TestWhatTheDeriverIsNotAskedFor:
    """Most of a plan is not the deriver's to decide, and asking for it back is
    a way of getting a different answer."""

    def test_the_format_comes_from_the_work(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget())

        assert held.target_format == work.target_format

    def test_the_title_comes_from_the_part(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        assert planned(SETTLED, work, node, (), LengthBudget()).title == node.title

    def test_each_editorial_section_description_reaches_the_writer_plan(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)
        brief = ComposedBrief(
            thesis="Agentic campaigns change the threat model.",
            opening=EditorialSection(
                title="The live campaign",
                establishes="The full attack chain can be automated.",
                order_reason="The current incident is the reader's pressing question.",
                evidence=["The Mythos system card and public incident record."],
                handoff="The reader can now compare capability with older failures.",
                key_points=["Name the observed campaign before historical context."],
            ),
        )

        section = planned(brief, work, node, (), LengthBudget()).sections[0]

        assert "full attack chain" in section.purpose
        assert "pressing question" in section.purpose
        assert section.evidence == [
            "The Mythos system card and public incident record."
        ]
        assert "compare capability" in section.handoff


class TestTheContractTheRunIsMeasuredOn:
    """`deliverables` is what every stage after the plan is checked against, so
    what a part run owes is stated there once."""

    def test_the_part_alone_with_its_own_heading_is_owed(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget())

        assert any("opening with its own heading" in one for one in held.deliverables)

    def test_the_length_range_is_carried_typed(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget(holds=900))

        assert held.word_budget is not None
        assert (held.word_budget.minimum, held.word_budget.maximum) == (600, 1350)

    def test_every_figure_is_owed_by_the_number_the_book_gave_it(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (FIGURE,), LengthBudget())

        assert any("Figure 2.13" in one for one in held.deliverables)

    def test_the_length_is_advised_and_not_owed(self, tmp_path: Path) -> None:
        """Nothing enforces the target, so stating it as a deliverable makes
        every stage but the last one act on a gate that is not there: the
        writer treats it as scope the author asked for, and the narrative
        reviewer raises the overrun as critical."""
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget(holds=1000))

        assert not any("words" in one for one in held.deliverables)
        advice = [one for one in held.constraints if "1,500 words" in one]
        assert len(advice) == 1
        assert "nothing rejects a piece outside it" in advice[0]

    def test_a_part_with_no_text_is_advised_no_length(self, tmp_path: Path) -> None:
        """A range around zero would read as an instruction to write nothing."""
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget())

        assert not any("words" in one for one in held.deliverables)
        assert not any("words" in one for one in held.constraints)


class TestComposingKeepsWhatItComposed:
    """A part that came back at four times its length either ignored its budget
    or was never given one, and only the brief on disk says which."""

    @pytest.mark.asyncio
    async def test_the_brief_is_kept_in_the_runs_own_room(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)
        room = tmp_path / "room"
        room.mkdir()

        await compose(
            work,
            node,
            evidence_for(work, node, WorkFindings()),
            room,
            writer=Answers(SETTLED),
        )

        kept = ArticlePlan.model_validate_json(
            (room / BRIEF_FILE).read_text(encoding="utf-8")
        )
        assert kept.thesis == SETTLED.thesis

    @pytest.mark.asyncio
    async def test_a_deriver_that_declines_returns_no_manuscript_plan(
        self, tmp_path: Path
    ) -> None:
        """The runner treats this as terminal before opening the pipeline."""
        work, node = part_of(tmp_path)
        room = tmp_path / "room"
        room.mkdir()

        held = await compose(
            work,
            node,
            evidence_for(work, node, WorkFindings()),
            room,
            writer=Answers(None),
        )

        assert held is None
        assert not (room / BRIEF_FILE).exists()

    @pytest.mark.asyncio
    async def test_the_parts_own_findings_reach_the_deriver(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)
        room = tmp_path / "room"
        room.mkdir()
        writer = Answers(SETTLED)
        found = WorkFindings(
            bearings=(
                Bearing(
                    key="02/03/2.3.2",
                    document="d",
                    claim="Cyber-range performance doubles every five months.",
                ),
            )
        )

        await compose(
            work,
            node,
            evidence_for(work, node, found),
            room,
            writer=writer,
        )

        assert "doubles every five months" in writer.asked[0]

    @pytest.mark.asyncio
    async def test_another_parts_findings_do_not(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)
        room = tmp_path / "room"
        room.mkdir()
        writer = Answers(SETTLED)
        found = WorkFindings(
            bearings=(
                Bearing(key="02/03/2.3.1", document="d", claim="Something about bio."),
            )
        )

        await compose(
            work,
            node,
            evidence_for(work, node, found),
            room,
            writer=writer,
        )

        assert "Something about bio." not in writer.asked[0]


class TestThePlanStageIsAnsweredRatherThanSkipped:
    """Skipping would have saved exactly the same planner call and taken the
    Plan tab, the section tabs, and the plan on disk with it silently."""

    def test_a_run_handed_a_plan_still_runs_the_plan_stage(self) -> None:
        held = PipelineRunner(
            sources=["a.md"],
            plan=ArticlePlan(
                title="T",
                thesis="X",
                target_format="textbook",
                sections=[],
                research_questions=[],
                source_quotes=[],
                author_direction="",
                voice_notes="",
            ),
        )

        assert "plan" in held.stages_for_run()

    def test_a_run_handed_nothing_plans_for_itself(self) -> None:
        assert PipelineRunner(sources=["a.md"]).launched_plan is None
