"""The light pipeline trims the backbone and the LinkedIn format opts into it.

A light run must skip the heavy stages (voice, deep research, assumptions,
refine, the parallel-section merge, resolve, rewrite) while keeping the spine
that drafts and fact-checks. The LinkedIn format turns this on without an
explicit flag — including on resume, where the format is recovered from the
snapshot's plan rather than the (reconstructed-as ``auto``) constructor arg.
"""

from pathlib import Path

from inkwell.agent.models import ArticlePlan
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import DISPLAY_STAGES, LIGHT_STAGES, PipelineRunner


def make_runner(
    tmp_path: Path, *, target_format: str = "auto", light: bool = False
) -> PipelineRunner:
    return PipelineRunner(
        sources=["x"],
        notes=PipelineNotes(tmp_path / "n"),
        target_format=target_format,
        light=light,
    )


def test_full_run_uses_the_whole_backbone(tmp_path: Path) -> None:
    r = make_runner(tmp_path)
    assert r.light is False
    assert r.stages_for_run() == ["preprocess", *DISPLAY_STAGES]


def test_light_run_trims_to_the_light_backbone(tmp_path: Path) -> None:
    r = make_runner(tmp_path, light=True)
    assert r.light is True
    assert r.stages_for_run() == ["preprocess", *LIGHT_STAGES]


def test_light_skips_the_heavy_stages_but_keeps_the_spine(tmp_path: Path) -> None:
    stages = make_runner(tmp_path, light=True).stages_for_run()
    heavy = {
        "voice",
        "research",
        "assumptions",
        "refine",
        "merge",
        "resolve",
        "rewrite",
    }
    assert heavy.isdisjoint(stages)
    for stage in ("extract", "plan", "write", "review", "format"):
        assert stage in stages


def test_linkedin_format_implies_light(tmp_path: Path) -> None:
    r = make_runner(tmp_path, target_format="linkedin")
    assert r.light is True
    assert r.stages_for_run() == ["preprocess", *LIGHT_STAGES]


def test_resume_recovers_light_from_the_plan_format(tmp_path: Path) -> None:
    # Resume reconstructs with target_format="auto"; lightness must come back
    # from the snapshot's plan so the remaining stages stay trimmed.
    r = make_runner(tmp_path, target_format="auto")
    assert r.light is False
    r.snapshot.plan = ArticlePlan(
        title="t",
        thesis="t",
        target_format="linkedin",
        author_direction="",
        voice_notes="",
        sections=[],
        research_questions=[],
        source_quotes=[],
    )
    assert r.light is True
    assert r.stages_for_run() == ["preprocess", *LIGHT_STAGES]
