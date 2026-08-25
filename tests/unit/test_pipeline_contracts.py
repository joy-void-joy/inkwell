"""Typed contracts that distinguish a manuscript-part run from an article."""

import asyncio
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


def wave(monkeypatch: pytest.MonkeyPatch, **outcomes: object) -> None:
    """Stand in for each reviewer, returning findings or raising what it is given."""

    def reviewer(outcome: object):
        async def review(*_args: object, **_kwargs: object) -> ReviewOutput:
            if isinstance(outcome, BaseException):
                raise outcome
            return ReviewOutput(findings=[])

        return review

    for name, outcome in outcomes.items():
        monkeypatch.setattr(pipeline, f"review_{name}", reviewer(outcome))
    monkeypatch.setattr(pipeline, "load_source_registry", lambda _path: [])


async def test_a_reviewer_that_did_not_return_fails_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Findings are the only thing review makes, so a short wave has no result
    to report: an empty list and a draft nobody objected to are the same bytes,
    and the rewrite reads the silence as approval. The run that lost all three
    reviewers to an expired token annotated nothing and shipped a misattributed
    statistic the same fact-checker had caught the run before."""
    wave(
        monkeypatch,
        narrative=ReviewOutput(findings=[]),
        facts=RuntimeError("OAuth session expired and could not be refreshed"),
        style=ReviewOutput(findings=[]),
    )

    with pytest.raises(pipeline.WaveIncomplete) as failed:
        await pipeline.review_all(
            PipelineNotes(tmp_path / "notes"),
            tmp_path / "draft.md",
            profile="manuscript_part",
            cohort=run_cohort(tmp_path / "cohort"),
        )

    assert failed.value.lost == ("facts",)
    assert failed.value.stage == "review"
    assert "resume the run" in str(failed.value)


async def test_an_interrupted_review_is_not_reported_as_a_lost_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wave hands a cancellation back positionally like any other outcome.
    Folding one into "the fact-checker failed" would report a run somebody
    stopped as a run that decided something, and would tell them to resume over
    a draft they had just interrupted."""
    wave(
        monkeypatch,
        narrative=ReviewOutput(findings=[]),
        facts=asyncio.CancelledError(),
        style=ReviewOutput(findings=[]),
    )

    with pytest.raises(asyncio.CancelledError):
        await pipeline.review_all(
            PipelineNotes(tmp_path / "notes"),
            tmp_path / "draft.md",
            profile="manuscript_part",
            cohort=run_cohort(tmp_path / "cohort"),
        )


def test_a_wave_that_came_back_whole_is_every_result_in_order() -> None:
    """The ordinary case, and the one the rule must not disturb."""
    assert pipeline.came_back_whole("review", ["a", "b"], ["one", "two"]) == [
        "one",
        "two",
    ]


def test_a_lost_worker_costs_its_stage_rather_than_only_itself() -> None:
    """Every fan-out stage answers to this, because a stage reporting what came
    back cannot say whether the rest disagreed or never ran. Review's silence
    reads as approval; a lost section ships the string "[Section failed: ...]"
    into the prose."""
    with pytest.raises(pipeline.WaveIncomplete) as failed:
        pipeline.came_back_whole(
            "write", ["intro", "body"], ["drafted", RuntimeError("gone")]
        )

    assert failed.value.stage == "write"
    assert failed.value.lost == ("body",)


def test_a_cancelled_worker_is_re_raised_rather_than_named_as_lost() -> None:
    """Reported as a lost worker it would read as a run that decided
    something, and would invite somebody to resume over a run they stopped."""
    with pytest.raises(asyncio.CancelledError):
        pipeline.came_back_whole("review", ["facts"], [asyncio.CancelledError()])


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


async def test_the_refiner_is_told_the_room_the_plan_has_to_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refiner may add sections research revealed as necessary, and nothing
    downstream can undo an over-commissioned plan: the writer is told to deliver
    it entire, so the overrun stops being a choice by the time anyone can count
    the words. Carrying the budget on the plan is not the same as saying it —
    a run whose length sat only in an advisory constraint came back with seven
    sections against room for five."""
    notes = PipelineNotes(tmp_path / "notes")
    notes.save_artifact(
        "plan", article_plan(budget=WordBudget(minimum=682, maximum=1534))
    )
    asked: list[str] = []

    async def refine(task: str, *_args: object, **_kwargs: object) -> None:
        asked.append(task)

    monkeypatch.setattr(pipeline, "query", refine)

    await pipeline.refine_plan(notes)

    assert "allocated 682 to 1,534 words" in asked[0]
    assert "makes the overrun mandatory" in asked[0]


async def test_a_plan_with_no_budget_tells_the_refiner_nothing_about_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A piece nobody allocated room to has no allocation to plan inside, and
    an interval invented here would be this stage answering a question about
    the work's shape that it is in no position to ask."""
    notes = PipelineNotes(tmp_path / "notes")
    notes.save_artifact("plan", article_plan())
    asked: list[str] = []

    async def refine(task: str, *_args: object, **_kwargs: object) -> None:
        asked.append(task)

    monkeypatch.setattr(pipeline, "query", refine)

    await pipeline.refine_plan(notes)

    assert "allocated" not in asked[0]


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
