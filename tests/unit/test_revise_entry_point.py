"""The revise entry point: an existing draft through the ordinary pipeline.

What makes revise a revision is one declared instruction, not a branch. These
pin that it reaches every surface like any other entry point, that the draft
enters the pipeline as ordinary material, and that the instruction riding
beside it asks for the currency check the entry point exists for.
"""

from pathlib import Path

from inkwell.agent.extract_agent import material_role_note
from inkwell.agent.pipeline import DISPLAY_STAGES, PipelineRunner
from inkwell.agent.tools.source_consult import SourceDocument, build_source_registry
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

    def test_it_keeps_the_author_s_voice_and_argument(self) -> None:
        """Those two, and deliberately not the structure: a revision that may
        not reorder or cut is a re-prose."""
        assert "voice and the argument" in REVISE_INSTRUCTION
        assert "yours to change" in REVISE_INSTRUCTION

    def test_it_says_the_draft_is_the_piece_being_replaced(self) -> None:
        assert "REPLACES" in REVISE_INSTRUCTION

    def test_published_prose_is_not_a_reason_to_tread_lightly(self) -> None:
        """The failure this instruction exists to stop: a run pointed at a
        textbook chapter reading its polish as a reason to leave it alone."""
        assert "not a reason to tread lightly" in REVISE_INSTRUCTION


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


class TestTheDraftTravelsUnderItsOwnRole:
    """The declaration the preservation reviewers read.

    The instruction alone cannot stop them: to a fidelity check every intended
    change reads as a departure from the source, and to a coverage check every
    cut reads as a dropped specific. Both consult the role instead.
    """

    def test_revise_declares_its_material_a_revision_target(self) -> None:
        assert REVISE.material_role == "revision_target"

    def test_every_other_entry_point_supplies_ordinary_source(self) -> None:
        others = [e for e in ENTRY_POINTS if e is not REVISE]

        assert all(e.material_role == "source" for e in others)

    def test_the_role_reaches_the_extraction_agent(self) -> None:
        """Declared rather than inferred: published prose carrying its own
        citations reads the same as a reference to write from."""
        note = material_role_note("revision_target")

        assert "revision_target" in note
        assert material_role_note("source") == ""


class TestARevisionTargetIsNotAnAuthority:
    """Consultable, so a writer can read the passage it is rewriting; never
    the document the finished draft is checked against."""

    def test_an_ordinary_source_is_authoritative(self) -> None:
        assert SourceDocument(label="paper", path="/p.pdf", kind="pdf").authoritative

    def test_the_piece_being_replaced_is_not(self) -> None:
        document = SourceDocument(
            label="chapter2", path="/c.md", kind="text", role="revision_target"
        )

        assert not document.authoritative

    def test_the_registry_marks_the_paths_the_run_replaces(
        self, tmp_path: Path
    ) -> None:
        draft = tmp_path / "chapter2.md"
        draft.write_text("published prose", encoding="utf-8")
        other = tmp_path / "paper.md"
        other.write_text("a real source", encoding="utf-8")

        registered = build_source_registry(
            [str(draft), str(other)], tmp_path / "artifacts", [str(draft)]
        )

        by_label = {d.label: d for d in registered}
        assert not by_label["chapter2"].authoritative
        assert by_label["paper"].authoritative
