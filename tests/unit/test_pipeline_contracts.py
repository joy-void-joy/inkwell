"""Typed contracts that distinguish a manuscript-part run from an article."""

from pathlib import Path

import pytest

import inkwell.agent.pipeline as pipeline
from inkwell.agent.cohort import run_cohort
from inkwell.agent.diversity import DiversityReport
from inkwell.agent.models import (
    ArticlePlan,
    ResearchCompilation,
    ReviewOutput,
    WritingOutput,
    WordBudget,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.tools.source_consult import SourceDocument


def article_plan(*, budget: WordBudget | None = None) -> ArticlePlan:
    """A minimal complete plan for contract tests."""
    return ArticlePlan(
        title="Part",
        thesis="Claim",
        target_format="textbook",
        sections=[],
        research_questions=[],
        source_quotes=[],
        author_direction="",
        voice_notes="",
        word_budget=budget,
    )


async def test_manuscript_review_owns_only_three_independent_concerns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def reviewer(name: str):
        async def review(*_args: object, **_kwargs: object) -> ReviewOutput:
            called.append(name)
            return ReviewOutput(findings=[])

        return review

    monkeypatch.setattr(pipeline, "review_narrative", reviewer("narrative"))
    monkeypatch.setattr(pipeline, "review_facts", reviewer("facts"))
    monkeypatch.setattr(pipeline, "review_style", reviewer("style"))
    monkeypatch.setattr(pipeline, "review_coverage", reviewer("coverage"))
    material = SourceDocument(
        label="standing", path="/standing.md", kind="text", factual_authority=False
    )
    monkeypatch.setattr(pipeline, "load_source_registry", lambda _path: [material])
    monkeypatch.setattr(pipeline, "review_source_fidelity", reviewer("fidelity"))

    await pipeline.review_all(
        PipelineNotes(tmp_path / "notes"),
        tmp_path / "draft.md",
        profile="manuscript_part",
        cohort=run_cohort(tmp_path / "cohort"),
    )

    assert set(called) == {"narrative", "facts", "style"}


async def test_manuscript_citation_diversity_costs_only_the_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = pipeline.PipelineRunner(
        sources=["part.md"],
        notes=PipelineNotes(tmp_path / "notes"),
        review_profile="manuscript_part",
    )
    runner.snapshot.research = ResearchCompilation(findings=[])
    judged: list[bool] = []

    async def check(*_args: object, judge: bool, **_kwargs: object) -> DiversityReport:
        judged.append(judge)
        return DiversityReport()

    monkeypatch.setattr(pipeline, "check_citation_diversity", check)

    await runner.check_diversity()

    assert judged == [False]


def test_word_budget_advises_on_a_final_candidate_outside_its_target() -> None:
    budget = WordBudget(minimum=2, maximum=3)

    inside = pipeline.counted("one two three", budget)
    assert inside.words == 3
    assert inside.advisory == ""

    over = pipeline.counted("one two three four", budget)
    assert over.words == 4
    assert "over" in over.advisory
    assert "4 words" in over.advisory


async def test_non_authoritative_material_cannot_fall_back_to_self_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = pipeline.PipelineRunner(
        sources=["part.md"], notes=PipelineNotes(tmp_path / "notes")
    )
    material = SourceDocument(
        label="standing", path="/standing.md", kind="text", factual_authority=False
    )
    monkeypatch.setattr(pipeline, "load_source_registry", lambda _path: [material])

    with pytest.raises(pipeline.PipelineError, match="requires an external plan"):
        await runner.stage_plan()


async def test_final_submission_saves_an_overrun_and_says_it_overran(
    tmp_path: Path,
) -> None:
    output = tmp_path / "final.md"
    tool = pipeline.make_final_submission_tool(output, WordBudget(minimum=2, maximum=3))

    over = await tool.call_handler(
        pipeline.SubmitFinalInput(content="one two three four")
    )
    assert over.word_count == 4
    assert "4 words" in over.advisory
    assert output.read_text(encoding="utf-8") == "one two three four"

    inside = await tool.call_handler(pipeline.SubmitFinalInput(content="one two"))
    assert inside.word_count == 2
    assert inside.advisory == ""
    assert output.read_text(encoding="utf-8") == "one two"


async def test_refinement_cannot_drop_the_launch_word_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = PipelineNotes(tmp_path / "notes")
    budget = WordBudget(minimum=100, maximum=150)
    notes.save_artifact("plan", article_plan(budget=budget))

    async def refine(*_args: object, **_kwargs: object) -> None:
        refined = article_plan().model_copy(update={"title": "Refined"})
        notes.artifact_path("plan_refined").write_text(
            refined.model_dump_json(), encoding="utf-8"
        )

    monkeypatch.setattr(pipeline, "query", refine)

    refined = await pipeline.refine_plan(notes)

    assert refined.word_budget == budget


async def test_format_checks_measure_the_actual_formatted_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = PipelineNotes(tmp_path / "notes")
    runner = pipeline.PipelineRunner(sources=["part.md"], notes=notes)
    runner.snapshot.plan = article_plan(budget=WordBudget(maximum=10))
    runner.snapshot.output = WritingOutput(title="Part", content="draft text")
    measured: list[str] = []

    async def formatted(*_args: object, **_kwargs: object) -> str:
        return "formatted final text"

    async def checked(
        _manifest: object, _notes: object, draft: str, _path: Path, **_kwargs: object
    ) -> None:
        measured.append(draft)

    async def quiet(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(pipeline, "apply_format", formatted)
    monkeypatch.setattr(pipeline, "add_format_check_report", checked)
    monkeypatch.setattr(runner, "announce_stage", quiet)
    monkeypatch.setattr(runner, "update_overview", quiet)
    monkeypatch.setattr(runner, "save_snapshot", quiet)
    monkeypatch.setattr(
        "inkwell.agent.tools.google_docs.write_with_continuation", quiet
    )

    await runner.stage_format()

    assert measured == ["formatted final text"]
    assert runner.snapshot.output.word_count == 3
