"""Tests for pipeline utility functions: slugify, voice refs."""

from pathlib import Path
from string import ascii_lowercase, digits
from typing import Literal

import pytest

from inkwell.agent.content import ContentManifest
from inkwell.agent.models import ArticlePlan, ResearchCompilation, ReviewFinding
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    academic_assembly_block,
    add_voice_refs,
    annotate_draft_with_findings,
    finding_tag,
    author_context_block,
    brief_block,
    consolidate_findings,
    corpus_briefing,
    planning_topic,
    render_brief,
    resolve_writer_mode,
    slugify,
    suggested_additions_block,
)
from inkwell.agent.stages import get_format_guidance
from inkwell.agent.tools.stage_outputs import PlanCollector, SetPlanHeaderInput
from inkwell.corpus.semantics import SemanticLayer
from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument
from inkwell.corpus.tags import DocumentTags


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

    def test_a_section_writers_label_is_a_name_docker_accepts(self) -> None:
        """What a stage's container is named after. A section heading is prose
        — spaces, a question mark, brackets — and Docker takes none of them,
        so the stage lost its sandbox and ran on unable to execute code."""
        legal = set(ascii_lowercase + digits + "_.-")

        slug = slugify("write:How much time do defenders have? (new paragraph)")

        assert set(slug) <= legal
        assert slug[0] not in "_.-"
        assert slug == "writehow-much-time-do-defenders-have-new-paragraph"


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


class TestCorpusBriefingReachesThePlanner:
    """What the corpus holds, pushed rather than left to a tool call.

    `corpus_search` was available to the research stage all along. A stage
    reaches for it once it knows there is something to look for, which is
    exactly what a piece working from an older draft does not know.
    """

    async def test_an_empty_corpus_adds_nothing_to_the_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_root", lambda: tmp_path / "corpus"
        )

        assert await corpus_briefing("AI and cyber risk") == ""

    async def test_the_briefing_says_how_its_results_were_ordered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A neighbour query falls back to the structural ordering where the
        semantic layer cannot answer, and calling the result "nearest" either way
        would tell the planner these were the closest documents on its subject
        when they were the most recent ones on any subject."""
        store = CorpusStore(root=tmp_path / "corpus")
        store.save(
            SourceShard(
                source="aisi",
                documents=[
                    StoredDocument(
                        slug="a-post",
                        url="https://fixture.test/a-post",
                        title="A post",
                        kind="markdown",
                        abstract="Prose about cyber risk.",
                        tags=DocumentTags(judged=True),
                    )
                ],
            )
        )
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_root", lambda: tmp_path / "corpus"
        )
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_semantics",
            lambda held: SemanticLayer(store=held, enabled=False),
        )

        briefing = await corpus_briefing("AI and cyber risk")

        assert "A post" in briefing
        assert "nearest first" not in briefing

    async def test_the_planner_is_shown_what_each_document_claims(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The judging pass already paid to read every document and wrote down
        what it says. A briefing of titles over the top of that spends the
        reading and throws the result away — and a title is exactly the part
        that cannot distinguish a document that moves something from one that
        does not."""
        store = CorpusStore(root=tmp_path / "corpus")
        store.save(
            SourceShard(
                source="aisi",
                documents=[
                    StoredDocument(
                        slug="evaluations",
                        url="https://fixture.test/evaluations",
                        title="Pre-deployment evaluations",
                        kind="markdown",
                        summary="Cyber-range performance is doubling every five months.",
                        tags=DocumentTags(judged=True),
                    )
                ],
            )
        )
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_root", lambda: tmp_path / "corpus"
        )
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_semantics",
            lambda held: SemanticLayer(store=held, enabled=False),
        )

        briefing = await corpus_briefing("AI and cyber risk")

        assert "doubling every five months" in briefing

    async def test_a_document_nothing_judged_is_named_rather_than_listed_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gap with a repair. Listed as a bare title it would read exactly
        like a document that had been read and found to say nothing."""
        store = CorpusStore(root=tmp_path / "corpus")
        store.save(
            SourceShard(
                source="aisi",
                documents=[
                    StoredDocument(
                        slug="unjudged",
                        url="https://fixture.test/unjudged",
                        title="Something nothing has read",
                        kind="markdown",
                    )
                ],
            )
        )
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_root", lambda: tmp_path / "corpus"
        )
        monkeypatch.setattr(
            "inkwell.agent.pipeline.corpus_semantics",
            lambda held: SemanticLayer(store=held, enabled=False),
        )

        briefing = await corpus_briefing("AI and cyber risk")

        assert "not yet judged" in briefing

    def test_the_brief_leads_the_subject_where_there_is_one(
        self, tmp_path: Path
    ) -> None:
        """An author's brief says what the piece is meant to be, which is the
        sharper description of the two, so it goes first."""
        notes = PipelineNotes(tmp_path / "notes")
        notes.save_brief("A textbook chapter on AI and cyber risk.")

        assert planning_topic(notes, "textbook").startswith("A textbook chapter")

    def test_the_material_is_asked_about_as_well_as_the_brief(
        self, tmp_path: Path
    ) -> None:
        """What distinguishes two parts of one work. A brief composed for the
        run rather than by a person says the same thing about every part it
        launches, so a corpus asked with the brief alone returns one list for a
        whole book and the planner cannot tell the parts apart."""
        notes = PipelineNotes(tmp_path / "notes")
        notes.save_brief("You are revising one part of a larger work.")
        path = notes.text_artifact_path("conversation")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## 2.3.2 Cyber Risk\n\nOffence-defence balance.", "utf-8")

        asked = planning_topic(notes, "textbook")

        assert "revising one part" in asked
        assert "Offence-defence" in asked

    def test_the_source_stands_in_when_no_brief_was_given(self, tmp_path: Path) -> None:
        notes = PipelineNotes(tmp_path / "notes")
        path = notes.text_artifact_path("conversation")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Cyber risk and the offense-defence balance.", "utf-8")

        assert "offense-defence" in planning_topic(notes, "textbook")

    def test_a_run_with_neither_falls_back_rather_than_failing(
        self, tmp_path: Path
    ) -> None:
        """A missing corpus subject is not a reason to end a run."""
        notes = PipelineNotes(tmp_path / "notes")

        assert planning_topic(notes, "textbook") == "textbook"


class TestFindingsCarryATagToAnswerBy:
    """The rewrite is handed ~70 findings and accounted for them in one
    sentence. A tag is what lets it answer each one, and what lets the audit
    read its answer instead of guessing from whether a passage survived."""

    def finding(
        self,
        excerpt: str,
        severity: Literal["critical", "suggestion", "praise"] = "critical",
    ) -> ReviewFinding:
        return ReviewFinding(
            reviewer="factcheck",
            severity=severity,
            location="§1",
            issue="wrong",
            text_excerpt=excerpt,
        )

    def test_tags_are_positional_and_stable(self) -> None:
        """So a resumed run's tag points at the same finding it did before."""
        assert finding_tag(0) == "F01"
        assert finding_tag(11) == "F12"

    def test_an_anchored_finding_carries_its_tag_into_the_draft(self) -> None:
        annotated = annotate_draft_with_findings(
            "The claim stands here.", [self.finding("The claim stands here.")]
        )

        assert "[F01:critical:factcheck]" in annotated

    def test_a_finding_that_cannot_anchor_still_carries_its_tag(self) -> None:
        """It lands in the trailing list, and the rewrite still has to answer
        it — an unanchorable finding is not an excused one."""
        annotated = annotate_draft_with_findings(
            "Some prose.", [self.finding("a passage that is not in the draft")]
        )

        assert "[F01:critical:factcheck]" in annotated

    def test_praise_carries_no_tag(self) -> None:
        """There is nothing to answer for beyond not spoiling the passage."""
        annotated = annotate_draft_with_findings(
            "Good line.", [self.finding("Good line.", severity="praise")]
        )

        assert "[PRESERVE:factcheck]" in annotated
        assert "F01" not in annotated
