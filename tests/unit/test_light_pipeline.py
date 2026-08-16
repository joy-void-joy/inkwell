"""The light pipeline trims the backbone and the LinkedIn format opts into it.

A light run must skip the heavy stages (voice, deep research, assumptions,
refine, the parallel-section merge, resolve, rewrite) while keeping the spine
that drafts and fact-checks. The LinkedIn format turns this on without an
explicit flag — including on resume, where the format is recovered from the
snapshot's plan rather than the (reconstructed-as ``auto``) constructor arg.

``light`` is also the parameter whose absence from the CLI showed that the three
entry point surfaces had drifted, so the last class here follows it through the
rendered path from both the command line and the API: the values a compiled
command collects and the values a request body validates to must reach the
pipeline as the same trimmed run.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

import inkwell.environment.launch as launch_module
from inkwell.agent.client import CostAccumulator
from inkwell.agent.core import SessionTrace
from inkwell.agent.models import AgentSessionResult, ArticlePlan, WritingOutput
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    DISPLAY_STAGES,
    LIGHT_STAGES,
    PipelineListener,
    PipelineRunner,
)
from inkwell.agent.session import WritingSessionState
from inkwell.environment.entrypoints import EntryPointValues
from inkwell.environment.launch import run_declared_session
from inkwell.environment.web.models import CreateSessionRequest


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


type LaunchArgument = (
    str
    | bool
    | list[str]
    | None
    | PipelineListener
    | SessionTrace
    | WritingSessionState
    | CostAccumulator
)
"""Anything the launch path hands the pipeline: a value the author declared, or
one of the run's collaborators."""


class PipelineCall(BaseModel):
    """What the launch path asked the pipeline for, captured instead of run."""

    sources: list[str] = Field(default_factory=list)
    light: bool = False
    target_format: str = "auto"


@pytest.fixture
def pipeline_call(monkeypatch: pytest.MonkeyPatch) -> PipelineCall:
    """Record the run the launch path starts, without starting one."""
    call = PipelineCall()

    async def record(
        *,
        sources: list[str] | None = None,
        target_format: str = "auto",
        light: bool = False,
        **rest: LaunchArgument,
    ) -> AgentSessionResult:
        call.sources = list(sources or [])
        call.target_format = target_format
        call.light = light
        return AgentSessionResult(
            session_id="test",
            timestamp="",
            output=WritingOutput(title=""),
        )

    monkeypatch.setattr(launch_module, "run_session", record)
    return call


async def launched(values: EntryPointValues) -> None:
    """Drive one entry point's values through the shared launch path."""
    await run_declared_session(values, session_id="test")


class TestLightReachesThePipelineThroughTheRenderedPath:
    """Both surfaces collect ``light`` under its declared name and it arrives."""

    async def test_from_the_command_line(self, pipeline_call: PipelineCall) -> None:
        # Exactly the values the compiled `inkwell write --light` command builds.
        await launched(
            EntryPointValues.declared(
                "write", {"sources": ["conversation.md"], "light": True}
            )
        )
        assert pipeline_call.light is True
        assert pipeline_call.sources == ["conversation.md"]

    async def test_from_the_api(self, pipeline_call: PipelineCall) -> None:
        body = CreateSessionRequest.model_validate(
            {"sources": ["conversation.md"], "light": True}
        )
        await launched(EntryPointValues.declared("write", body.model_dump(mode="json")))
        assert pipeline_call.light is True

    async def test_a_run_without_it_is_not_light(
        self, pipeline_call: PipelineCall
    ) -> None:
        await launched(
            EntryPointValues.declared("write", {"sources": ["conversation.md"]})
        )
        assert pipeline_call.light is False

    async def test_the_linkedin_format_still_implies_it(
        self, pipeline_call: PipelineCall, tmp_path: Path
    ) -> None:
        await launched(
            EntryPointValues.declared(
                "write", {"sources": ["x"], "target_format": "linkedin"}
            )
        )
        assert pipeline_call.light is False
        assert pipeline_call.target_format == "linkedin"
        # The format's implication is the runner's rather than the flag's, so
        # the trimmed backbone is what proves the format arrived.
        runner = make_runner(tmp_path, target_format=pipeline_call.target_format)
        assert runner.stages_for_run() == ["preprocess", *LIGHT_STAGES]


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
