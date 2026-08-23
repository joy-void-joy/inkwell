"""The plan a part run is handed, instead of the one it would derive.

A run holding one subsection planned that subsection's existing sections,
because that is all it could see. What is worth pinning is that the plan now
arrives from outside carrying what only the work knows — the budget, the
figures, what the run owes — that the plan stage *records* it rather than being
skipped over, and that a deriver which comes back with nothing leaves the run
planning for itself rather than failing.
"""

from pathlib import Path

import pytest

from inkwell.agent.models import ArticlePlan, SectionPlan
from inkwell.agent.pipeline import PipelineRunner
from inkwell.manuscript.brief import (
    BRIEF_FILE,
    BriefWriter,
    ComposedBrief,
    asked,
    compose,
    planned,
)
from inkwell.manuscript.budget import LengthBudget
from inkwell.manuscript.findings import Bearing, WorkFindings
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.inventory import Figure
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

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
    sections=[
        SectionPlan(title="What is measured", summary="The evaluations.", key_points=[])
    ],
    direction="Lead with the sandbox escape.",
)

FIGURE = Figure(
    number="Figure 2.13",
    image="Images/cJx_Image_13.png",
    caption="Stages of a cyberattack.",
    block="<figure>…</figure>",
)


class TestWhatTheDeriverIsShown:
    """One part, the two files either side of it, and this part's slice of what
    the corpus established — never the book."""

    def test_the_part_is_named_by_the_key_state_uses(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        said = asked(work, node, Path("/room/part.md"), "instruction", "")

        assert "02/03/2.3.2" in said

    def test_the_material_arrives_as_a_file_to_open(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        said = asked(work, node, Path("/room/part.md"), "instruction", "")

        assert "/room/part.md" in said

    def test_the_writers_own_instruction_is_what_the_planner_reads(
        self, tmp_path: Path
    ) -> None:
        """A plan made against a brief the writer never sees is a plan for a
        different run."""
        work, node = part_of(tmp_path)

        said = asked(work, node, Path("/room/part.md"), "Land under 1,500 words.", "")

        assert "Land under 1,500 words." in said

    def test_what_the_research_holds_travels_with_it(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        said = asked(work, node, Path("/room/part.md"), "", "## Research\n- A thing.")

        assert "- A thing." in said


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


class TestTheContractTheRunIsMeasuredOn:
    """`deliverables` is what every stage after the plan is checked against, so
    what a part run owes is stated there once."""

    def test_the_part_alone_with_its_own_heading_is_owed(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget())

        assert any("opening with its own heading" in one for one in held.deliverables)

    def test_every_figure_is_owed_by_the_number_the_book_gave_it(
        self, tmp_path: Path
    ) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (FIGURE,), LengthBudget())

        assert any("Figure 2.13" in one for one in held.deliverables)

    def test_the_length_is_owed_where_the_part_has_one(self, tmp_path: Path) -> None:
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget(holds=1000))

        assert any("1,500 words" in one for one in held.deliverables)

    def test_a_part_with_no_text_is_owed_no_length(self, tmp_path: Path) -> None:
        """A range around zero would read as an instruction to write nothing."""
        work, node = part_of(tmp_path)

        held = planned(SETTLED, work, node, (), LengthBudget())

        assert not any("words" in one for one in held.deliverables)


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
            tmp_path / "part.md",
            room,
            instruction="",
            writer=Answers(SETTLED),
        )

        kept = ArticlePlan.model_validate_json(
            (room / BRIEF_FILE).read_text(encoding="utf-8")
        )
        assert kept.thesis == SETTLED.thesis

    @pytest.mark.asyncio
    async def test_a_deriver_that_declines_leaves_the_run_to_plan_for_itself(
        self, tmp_path: Path
    ) -> None:
        """A brief is a better plan, not a required one. Losing a book pass to a
        planner that timed out would be the expensive way to hold that opinion.
        """
        work, node = part_of(tmp_path)
        room = tmp_path / "room"
        room.mkdir()

        held = await compose(
            work, node, tmp_path / "part.md", room, instruction="", writer=Answers(None)
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
            tmp_path / "part.md",
            room,
            instruction="",
            found=found,
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
            tmp_path / "part.md",
            room,
            instruction="",
            found=found,
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
