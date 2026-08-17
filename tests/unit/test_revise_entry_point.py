"""The revise entry point: an existing draft through the ordinary pipeline.

What makes revise a revision is one declared instruction, not a branch. These
pin that it reaches every surface like any other entry point, that the draft
enters the pipeline as ordinary material, and that the instruction riding
beside it asks for the currency check the entry point exists for.
"""

from inkwell.agent.pipeline import DISPLAY_STAGES, PipelineRunner
from inkwell.environment.entrypoints import (
    ENTRY_POINTS,
    REVISE,
    REVISE_INSTRUCTION,
    EntryPointValues,
    entry_point_named,
    request_model,
)


class TestItIsDeclaredLikeTheOthers:
    """One declaration reaches the CLI, the API and the form."""

    def test_it_is_in_the_one_roster(self) -> None:
        assert REVISE in ENTRY_POINTS
        assert entry_point_named("revise") is REVISE

    def test_it_starts_a_session(self) -> None:
        assert REVISE.starts_a_session

    def test_the_draft_reaches_every_surface(self) -> None:
        """No hand-written third copy: one parameter, three renderings."""
        assert "draft" in {p.name for p in REVISE.command_parameters}
        assert "draft" in {p.name for p in REVISE.api_parameters}
        assert "draft" in {p.name for p in REVISE.form_parameters}

    def test_its_request_model_is_built_from_the_declaration(self) -> None:
        assert "draft" in request_model(REVISE).model_fields


class TestTheDraftEntersTheOrdinaryPipeline:
    """No revise-only path: the draft is material like any other."""

    def test_the_draft_is_the_material(self) -> None:
        values = EntryPointValues.declared("revise", {"draft": "draft.md"})

        assert REVISE.sources(values)[0] == "draft.md"

    def test_the_standing_instruction_rides_beside_it(self) -> None:
        values = EntryPointValues.declared("revise", {"draft": "draft.md"})
        sources = REVISE.sources(values)

        assert sources[1] == REVISE_INSTRUCTION
        assert len(sources) == 2

    def test_it_runs_the_whole_backbone(self) -> None:
        """Extraction stays because a draft may be a Doc, a URL or a file;
        research stays because that is where currency is actually checked."""
        runner = PipelineRunner(
            sources=["draft.md"], skipped_stages=REVISE.skipped_stages
        )

        assert runner.stages_for_run() == DISPLAY_STAGES


class TestTheStandingInstructionIsData:
    """Declared once on the entry point, not embedded in a prompt string."""

    def test_it_is_declared_on_the_entry_point(self) -> None:
        assert REVISE.standing_instruction == REVISE_INSTRUCTION

    def test_no_other_entry_point_carries_one(self) -> None:
        """A second entry point wanting one declares it, rather than finding
        the prompt revise embedded its own in."""
        others = [e for e in ENTRY_POINTS if e is not REVISE]

        assert all(not getattr(e, "standing_instruction", "") for e in others)

    def test_it_asks_for_clarity_and_currency(self) -> None:
        assert "clarity and currency" in REVISE_INSTRUCTION

    def test_it_keeps_the_author_s_piece_theirs(self) -> None:
        assert "voice, argument, and structure" in REVISE_INSTRUCTION
        assert "not a new one" in REVISE_INSTRUCTION


class TestCurrencyIsCheckedAgainstTheDraft:
    """The point of revising rather than re-prosing."""

    def test_the_questions_come_from_the_draft_s_own_claims(self) -> None:
        assert "the draft's own claims asked again" in REVISE_INSTRUCTION

    def test_it_names_what_a_claim_rests_on(self) -> None:
        assert "fact, figure, and citation" in REVISE_INSTRUCTION

    def test_it_asks_whether_anything_superseded_them(self) -> None:
        assert "still holds" in REVISE_INSTRUCTION
        assert "superseded" in REVISE_INSTRUCTION

    def test_the_sections_are_the_draft_s(self) -> None:
        """Planned as the piece it already is, rather than planned afresh."""
        assert "its sections are the draft's sections" in REVISE_INSTRUCTION
