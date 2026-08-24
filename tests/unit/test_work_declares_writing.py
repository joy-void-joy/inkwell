"""What a work says about how its parts are written, once, at import.

The format already travelled this way, and for a reason that reaches past the
format: a run handed one subsection can answer any of these questions, and two
parts of one book answering differently is a book written two ways. The parallel
split is the expensive one — nine writers and a merge, the largest single line
of what a part costs, to draft what one writer drafts in one pass over material
short enough that no writer was ever short of context.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from inkwell.agent.models import AgentSessionResult, WritingOutput
from inkwell.agent.pipeline import PipelineRunner
from inkwell.manuscript import runner as runner_module
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.brief import (
    BriefWriter,
    ComposedBrief,
    CorpusPusher,
    EditorialSection,
    PushedCorpusClaim,
)
from inkwell.manuscript.inheritance import ADOPTED_FILE, Audit, InheritanceReader
from inkwell.manuscript.runner import PRODUCED_FILE, PartRunAgents, run_part
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import DEFAULT_WRITER_MODE, Manuscript

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


class PassesThrough(InheritanceReader):
    """Adopts the fresh draft verbatim, so no test here reaches a model."""

    async def read(self, task: str, room: Path) -> Audit:
        (room / ADOPTED_FILE).write_text(
            (room / PRODUCED_FILE).read_text(encoding="utf-8"), encoding="utf-8"
        )
        return Audit()


class PlansPart(BriefWriter):
    """Returns a prose-blind plan without reaching a model."""

    async def compose(self, task: str, root: Path) -> ComposedBrief:
        return ComposedBrief(
            thesis="The successor.",
            opening=EditorialSection(
                title="The successor",
                establishes="The planned point.",
                order_reason="It opens the part.",
                evidence=["The fixture."],
                handoff="The part can conclude.",
                key_points=[],
            ),
        )


class PushesNothing(CorpusPusher):
    """An empty corpus, so unit runs do no external retrieval."""

    async def push(
        self, topic: str, subject: str = ""
    ) -> tuple[PushedCorpusClaim, ...]:
        return ()


OFFLINE = PartRunAgents(
    briefing=PlansPart(), corpus=PushesNothing(), inheriting=PassesThrough()
)
"""Every reader a part run buys, stubbed — handed over as the set."""


def atlas_like(root: Path) -> Path:
    """A one-part work, small enough to read at once."""
    chapter = root / "chapters" / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    return root / "chapters"


async def asked_of_a_run(
    tmp_path: Path, work: Manuscript, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """What a part run of this work hands the pipeline."""
    node = work.node("02/03/2.3.2")
    assert node is not None
    asked: dict[str, object] = {}

    async def record(**passed: object) -> AgentSessionResult:
        asked.update(passed)
        return AgentSessionResult(
            session_id="s",
            timestamp="",
            output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
        )

    monkeypatch.setattr(runner_module, "run_session", record)
    await run_part(
        ManuscriptStore(root=tmp_path / "manuscripts"),
        "atlas",
        work,
        node,
        session_id="s",
        agents=OFFLINE,
    )
    return asked


class TestTheWorkSaysHowItsPartsAreDrafted:
    """A setting says it for every run of every project; a work says it for
    every run against this book."""

    def test_a_work_drafts_each_part_with_one_writer_by_default(
        self, tmp_path: Path
    ) -> None:
        """The book has already done the splitting. Splitting a subsection again
        bought eleven stage-runs to produce what one produces in one."""
        assert read_manuscript(atlas_like(tmp_path)).writer_mode == "single"

    def test_a_work_may_declare_the_parallel_split_anyway(self, tmp_path: Path) -> None:
        held = read_manuscript(atlas_like(tmp_path), writer_mode="parallel")

        assert held.writer_mode == "parallel"

    @pytest.mark.asyncio
    async def test_the_declaration_reaches_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path), writer_mode="parallel")

        asked = await asked_of_a_run(tmp_path, work, monkeypatch)

        assert asked["writer_mode"] == "parallel"


class TestTheRunReadsTheLaunchBeforeTheSetting:
    """Declared beats ambient, and no declaration leaves the ambient setting
    exactly as it was — otherwise adopting the seam moves every run."""

    def test_a_declared_mode_wins(self) -> None:
        held = PipelineRunner(sources=["a.md"], writer_mode="single")

        assert held.writer_mode() == "single"

    def test_auto_resolves_the_way_it_always_did(self) -> None:
        held = PipelineRunner(sources=["a.md"], writer_mode="auto")

        assert held.writer_mode() == "parallel"

    def test_no_declaration_falls_to_the_setting(self) -> None:
        held = PipelineRunner(sources=["a.md"])

        assert held.writer_mode() in ("single", "parallel")


class TestTheWorkSaysWhichStagesItsPartsSkip:
    """Which stages a book's parts need is a property of the book: one whose
    voice and assumptions are settled at the work level has its parts skip
    both, and one whose parts are drafts does not."""

    def test_a_work_settles_assumptions_by_default(self, tmp_path: Path) -> None:
        assert read_manuscript(atlas_like(tmp_path)).skipped_stages == ("assumptions",)

    def test_a_declared_skip_is_recorded(self, tmp_path: Path) -> None:
        held = read_manuscript(atlas_like(tmp_path), skipped_stages=("voice",))

        assert held.skipped_stages == ("assumptions", "voice")

    def test_an_older_tree_inherits_the_work_level_skip(self) -> None:
        held = Manuscript.model_validate(
            {"title": "W", "root": "/n", "skipped_stages": []}
        )

        assert held.skipped_stages == ("assumptions",)

    @pytest.mark.asyncio
    async def test_the_skips_reach_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(
            atlas_like(tmp_path), skipped_stages=("voice", "assumptions")
        )

        asked = await asked_of_a_run(tmp_path, work, monkeypatch)

        assert asked["skipped_stages"] == ["assumptions", "voice"]

    def test_a_skip_naming_no_stage_is_refused(self, tmp_path: Path) -> None:
        """A typo skips nothing and reads afterwards exactly like a stage that
        ran — the only evidence is a bill nobody was expecting."""
        with pytest.raises(ValidationError, match="which name no stage"):
            read_manuscript(atlas_like(tmp_path), skipped_stages=("voise",))

    def test_a_tree_edited_by_hand_is_refused_on_the_same_terms(self) -> None:
        with pytest.raises(ValidationError, match="which name no stage"):
            Manuscript(title="W", root="/nowhere", skipped_stages=("nonsense",))

    def test_the_default_mode_is_named_rather_than_repeated(self) -> None:
        """A second spelling of it is a second thing to keep in step."""
        assert Manuscript(title="W", root="/n").writer_mode == DEFAULT_WRITER_MODE
