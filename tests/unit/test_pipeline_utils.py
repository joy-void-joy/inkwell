"""Tests for pipeline utility functions: slugify, voice refs."""

from pathlib import Path

from inkwell.agent.content import ContentManifest
from inkwell.agent.models import ArticlePlan, ResearchCompilation, ReviewFinding
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    academic_assembly_block,
    add_voice_refs,
    author_context_block,
    brief_block,
    consolidate_findings,
    render_brief,
    resolve_writer_mode,
    slugify,
    suggested_additions_block,
)
from inkwell.agent.stages import get_format_guidance
from inkwell.agent.tools.stage_outputs import PlanCollector, SetPlanHeaderInput


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self) -> None:
        assert slugify("What's the point?") == "whats-the-point"

    def test_colons_and_slashes(self) -> None:
        assert slugify("format:lesswrong/example") == "formatlesswrongexample"

    def test_double_hyphens_collapsed(self) -> None:
        assert slugify("a -- b") == "a-b"

    def test_truncates_long_input(self) -> None:
        result = slugify("x" * 200)
        assert len(result) <= 80


class TestAddVoiceRefs:
    def test_empty_list(self) -> None:
        manifest = ContentManifest()
        add_voice_refs(manifest, [])
        assert manifest.render() == ""

    def test_voice_files(self) -> None:
        manifest = ContentManifest()
        add_voice_refs(manifest, ["/tmp/voice_0_conversation.md"])
        rendered = manifest.render()
        assert "voice_analysis" in rendered
        assert "/tmp/voice_0_conversation.md" in rendered

    def test_corpus_files(self) -> None:
        manifest = ContentManifest()
        add_voice_refs(manifest, ["/tmp/corpus_0_humanizer.md"])
        rendered = manifest.render()
        assert "style_reference" in rendered

    def test_prescriptive_files(self) -> None:
        manifest = ContentManifest()
        add_voice_refs(manifest, ["/tmp/prescriptive_0_rules.md"])
        rendered = manifest.render()
        assert "prescriptive_rules" in rendered
        assert "hard constraints" in rendered

    def test_mixed_files(self) -> None:
        manifest = ContentManifest()
        add_voice_refs(
            manifest,
            [
                "/tmp/voice_0_conversation.md",
                "/tmp/corpus_0_skill.md",
                "/tmp/prescriptive_0_guide.md",
            ],
        )
        rendered = manifest.render()
        assert "voice_analysis" in rendered
        assert "style_reference" in rendered
        assert "prescriptive_rules" in rendered


class TestResolveWriterMode:
    def test_auto_resolves_to_parallel(self) -> None:
        assert resolve_writer_mode("auto") == "parallel"

    def test_explicit_mode_passes_through(self) -> None:
        assert resolve_writer_mode("single") == "single"
        assert resolve_writer_mode("parallel") == "parallel"


class TestConsolidateFindingsCoverage:
    def test_every_distinct_suggestion_reaches_the_rewrite(self) -> None:
        """No finding is dropped to keep the list short.

        The rewriter is the only reader that can weigh a suggestion against
        the draft, so a suggestion it never sees is one the author loses with
        nothing said.
        """
        coverage = ReviewFinding(
            reviewer="coverage",
            severity="critical",
            location="§3",
            issue="the draft dropped the author's Mythos example",
            text_excerpt="Mythos",
        )
        suggestions = [
            ReviewFinding(
                reviewer="style",
                severity="suggestion",
                location=f"para {i}",
                issue="filler",
                text_excerpt=f"excerpt {i}",
            )
            for i in range(20)
        ]
        result = consolidate_findings([coverage, *suggestions])
        assert any(
            f.reviewer == "coverage" and f.severity == "critical" for f in result
        )
        assert len(result) == len(suggestions) + 1

    def test_two_reviewers_on_one_passage_fold_into_one_finding(self) -> None:
        """Folding a duplicate is not dropping it — both suggestions survive."""
        quoted = [
            ReviewFinding(
                reviewer=reviewer,
                severity="suggestion",
                location="§1",
                issue="hedged",
                text_excerpt="the same sentence",
                suggestion=f"{reviewer} says cut it",
            )
            for reviewer in ("style", "narrative")
        ]
        result = consolidate_findings(quoted)
        assert len(result) == 1
        assert "style says cut it" in result[0].suggestion
        assert "narrative says cut it" in result[0].suggestion


class TestFormatGuidancePrecedence:
    def test_no_format_prose_for_auto_and_unknown(self) -> None:
        """Nothing but the rows every format carries, which have no format prose."""
        for key in ("auto", "nonexistent"):
            assert "Format:" not in get_format_guidance(key)

    def test_guidance_defers_to_voice(self) -> None:
        assert "voice outranks" in get_format_guidance("memo").lower()
        assert "voice outranks" in get_format_guidance("lesswrong").lower()

    def test_memo_no_longer_mandates_bolding_every_sentence(self) -> None:
        guidance = get_format_guidance("memo")
        assert "sparingly" in guidance


class TestRenderBrief:
    def test_instructions_and_deliverables(self) -> None:
        rendered = render_brief("Keep it short.", ["a one-pager", "a chart"])
        assert "Keep it short." in rendered
        assert "- a one-pager" in rendered
        assert "- a chart" in rendered

    def test_empty_inputs(self) -> None:
        assert render_brief("", []) == ""

    def test_instructions_only(self) -> None:
        assert render_brief("Be terse.", []) == "Be terse."


class TestBriefPropagation:
    def test_absent_brief_renders_nothing(self, tmp_path) -> None:
        notes = PipelineNotes(tmp_path / "notes")
        assert brief_block(notes) == ""

    def test_brief_is_framed_as_the_contract(self, tmp_path) -> None:
        notes = PipelineNotes(tmp_path / "notes")
        notes.save_brief("Self-contained; do not cite the source.")
        block = brief_block(notes)
        assert "Self-contained; do not cite the source." in block
        assert "contract" in block.lower()

    def test_author_context_carries_the_brief(self, tmp_path) -> None:
        notes = PipelineNotes(tmp_path / "notes")
        notes.save_brief("Self-contained; do not cite the source.")
        assert "Self-contained" in author_context_block(notes)


class TestConstraintsFlowThroughPlan:
    def test_constraints_reach_the_loaded_plan(self, tmp_path) -> None:
        collector = PlanCollector(tmp_path / "plan.json")
        collector.plan = collector.plan.model_copy(
            update=SetPlanHeaderInput(
                title="T",
                thesis="th",
                target_format="academic",
                author_direction="d",
                deliverables=["the paper"],
                constraints=["self-contained; do not cite the source"],
                conventions=[],
                voice_notes="v",
            ).model_dump()
        )
        collector.save()
        plan = ArticlePlan.model_validate_json(
            (tmp_path / "plan.json").read_text(encoding="utf-8")
        )
        assert plan.constraints == ["self-contained; do not cite the source"]


class TestAcademicAssemblyBlock:
    def test_empty_for_non_academic(self) -> None:
        assert academic_assembly_block("lesswrong", Path("/x/out.md")) == ""
        assert academic_assembly_block("blog", Path("/x/out.md")) == ""
        assert academic_assembly_block("auto", Path("/x/out.md")) == ""

    def test_academic_owns_document_and_compiles(self) -> None:
        block = academic_assembly_block("academic", Path("/work/paper.tex"))
        assert "/work/paper.tex" in block
        assert "compile_latex" in block
        assert "\\newtheorem" in block
        assert "preamble" in block.lower()


class TestSuggestedAdditionsReachTheRefiner:
    """The channel for material no research question asked for.

    The researcher is told to file anything valuable that emerged outside the
    original questions. `suggested_additions` had one writer and no readers, so
    a proposal arriving only that way was collected and dropped — and it is the
    only route for a development the source predates, which has no claim in the
    source for a verification question to hang off.
    """

    def test_nothing_is_rendered_before_research_runs(self, tmp_path: Path) -> None:
        notes = PipelineNotes(tmp_path / "notes")

        assert suggested_additions_block(notes) == ""

    def test_a_run_that_suggested_nothing_renders_nothing(self, tmp_path: Path) -> None:
        notes = PipelineNotes(tmp_path / "notes")
        notes.save_artifact("research", ResearchCompilation(findings=[]))

        assert suggested_additions_block(notes) == ""

    def test_every_suggestion_reaches_the_refiner_whole(self, tmp_path: Path) -> None:
        """Whole, because a suggestion is an argument for a change — a trimmed
        one reads as a topic the refiner cannot weigh."""
        notes = PipelineNotes(tmp_path / "notes")
        first = "An agent breached Hugging Face in July 2026; the draft predates it."
        second = "The flash-attack term is from Staniford et al. 2002, not Fang."
        notes.save_artifact(
            "research",
            ResearchCompilation(findings=[], suggested_additions=[first, second]),
        )

        block = suggested_additions_block(notes)

        assert first in block
        assert second in block

    def test_the_refiner_must_answer_each_one(self, tmp_path: Path) -> None:
        """Adopt, fold in, or reject with a reason — silence was the old
        behaviour and is what this block exists to stop."""
        notes = PipelineNotes(tmp_path / "notes")
        notes.save_artifact(
            "research", ResearchCompilation(findings=[], suggested_additions=["a"])
        )

        block = suggested_additions_block(notes)

        assert "reject it with a reason" in block
        assert "Silence" in block
