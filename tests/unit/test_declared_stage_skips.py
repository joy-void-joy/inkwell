"""Stages an entry point declares its runs do not perform.

The shape being pinned is that the declaration is read in exactly one place.
A stage that learned to recognise which entry point launched it would work
just as well for the first entry point and leave the second to rediscover
every such conditional, which is the failure the declaration exists to
prevent.
"""

import pytest

from inkwell.agent.pipeline import DISPLAY_STAGES, LIGHT_STAGES, PipelineRunner
from inkwell.environment.entrypoints import ENTRY_POINTS, RESUME, WRITE


def stages_when(skipped: list[str], *, light: bool = False) -> list[str]:
    runner = PipelineRunner(sources=["draft.md"], light=light, skipped_stages=skipped)
    return runner.stages_for_run()


def test_a_declared_skip_leaves_the_run() -> None:
    assert "voice" not in stages_when(["voice"])


def test_every_other_stage_survives_a_skip() -> None:
    """A skip removes what it names and nothing near it."""
    kept = stages_when(["voice"])

    assert kept == ["preprocess", *[s for s in DISPLAY_STAGES if s != "voice"]]


def test_declaring_none_runs_the_whole_backbone() -> None:
    """The default has to be unchanged, or adopting the seam moves every run."""
    assert stages_when([]) == ["preprocess", *DISPLAY_STAGES]


def test_skips_compose_with_a_light_run() -> None:
    """Light already trims the backbone; a skip narrows what light left."""
    kept = stages_when(["plan"], light=True)

    assert kept == ["preprocess", *[s for s in LIGHT_STAGES if s != "plan"]]


def test_a_skip_naming_no_stage_is_refused() -> None:
    """A typo would otherwise skip nothing and read as a stage that ran."""
    with pytest.raises(ValueError, match="which name no stage"):
        type(WRITE)(
            name="typo",
            summary="Skips a stage that does not exist.",
            parameters=[],
            skipped_stages=["planning"],
        )


def test_every_declared_skip_names_a_real_stage() -> None:
    """The declarations that ship, checked against the pipeline's own list."""
    for entry_point in ENTRY_POINTS:
        assert set(entry_point.skipped_stages) <= set(DISPLAY_STAGES), entry_point.name


def test_a_continued_entry_point_skips_nothing() -> None:
    """Resume takes the stages its saved run left, so it declares none."""
    assert RESUME.skipped_stages == []
