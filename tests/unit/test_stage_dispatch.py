"""Every stage the runner sequences resolves to a method that runs it.

``run_stage`` dispatches through ``getattr(self, f"stage_{name}")``, so a name
in the backbone that no method answers is an AttributeError on the stage
boundary rather than a type error at import. Pinning the sequence's *shape*
does not catch that: a backbone can name a stage that was deleted and every
shape assertion stays green while the first stage of every fresh run raises.
This is the assertion that the sequence is executable at all.
"""

from inkwell.agent.config import PIPELINE_STAGES
from inkwell.agent.pipeline import (
    CHECKPOINT_STAGES,
    DISPLAY_STAGES,
    LIGHT_STAGES,
    PipelineRunner,
)


def undispatchable(runner: PipelineRunner) -> list[str]:
    return [s for s in runner.stages_for_run() if not hasattr(runner, f"stage_{s}")]


def test_a_full_run_can_dispatch_every_stage_it_sequences() -> None:
    assert undispatchable(PipelineRunner(sources=["draft.md"])) == []


def test_a_light_run_can_dispatch_every_stage_it_sequences() -> None:
    assert undispatchable(PipelineRunner(sources=["draft.md"], light=True)) == []


def test_the_light_backbone_is_a_subsequence_of_the_full_one() -> None:
    """Light trims stages; it never introduces one the full backbone lacks."""
    assert [s for s in DISPLAY_STAGES if s in LIGHT_STAGES] == LIGHT_STAGES


def test_every_checkpoint_target_is_a_stage_that_runs() -> None:
    """``resume --from`` and ``--stop-after`` name a boundary a run reaches."""
    assert set(CHECKPOINT_STAGES) <= set(DISPLAY_STAGES)


def test_every_backbone_stage_can_carry_a_model_override() -> None:
    """A stage absent from PipelineStage cannot be pointed at a model."""
    assert set(DISPLAY_STAGES) <= set(PIPELINE_STAGES)
