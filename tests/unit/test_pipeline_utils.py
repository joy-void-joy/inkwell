"""Tests for pipeline utility functions: slugify, research splitting, voice refs."""

from pathlib import Path

import pytest

from inkwell.agent.models import (
    ArticlePlan,
    ResearchCompilation,
    ResearchFinding,
    ResearchQuestion,
    ResearchSource,
    SectionPlan,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    build_voice_file_refs,
    render_finding_markdown,
    slugify,
    split_research_by_section,
)


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


class TestRenderFindingMarkdown:
    def test_basic_finding(self) -> None:
        finding = ResearchFinding(
            question="Is X true?",
            answer="Yes, X is true based on evidence.",
            sources=[ResearchSource(title="Paper", url="https://example.com", relevance="key", key_excerpt="quote")],
            confidence=0.85,
            data_points=["42%", "2024"],
        )
        md = render_finding_markdown(finding)
        assert "Is X true?" in md
        assert "Yes, X is true" in md
        assert "85%" in md
        assert "42%" in md
        assert "https://example.com" in md

    def test_no_data_points(self) -> None:
        finding = ResearchFinding(
            question="Q?",
            answer="A.",
            sources=[],
            confidence=0.5,
        )
        md = render_finding_markdown(finding)
        assert "Data points" not in md
        assert "50%" in md


class TestSplitResearchBySection:
    @pytest.fixture
    def notes(self, tmp_path: Path) -> PipelineNotes:
        return PipelineNotes(tmp_path / "pipeline_notes")

    @pytest.fixture
    def plan(self) -> ArticlePlan:
        return ArticlePlan(
            title="Test Article",
            thesis="Testing is important",
            target_format="blog",
            sections=[
                SectionPlan(title="Introduction", summary="Intro stuff", key_points=["a"]),
                SectionPlan(title="Body", summary="Main content", key_points=["b"]),
            ],
            research_questions=[
                ResearchQuestion(question="What is the background?", section="Introduction"),
                ResearchQuestion(question="How does the main thing work?", section="Body"),
            ],
            source_quotes=[],
            author_direction="",
            voice_notes="",
        )

    def test_splits_by_section(self, notes: PipelineNotes, plan: ArticlePlan) -> None:
        research = ResearchCompilation(
            findings=[
                ResearchFinding(
                    question="What is the background?",
                    answer="Historical context here.",
                    sources=[],
                    confidence=0.9,
                ),
                ResearchFinding(
                    question="How does the main thing work?",
                    answer="It works like this.",
                    sources=[],
                    confidence=0.8,
                ),
            ],
        )
        paths = split_research_by_section(research, plan, notes)
        assert "Introduction" in paths
        assert "Body" in paths
        intro_content = paths["Introduction"].read_text()
        assert "background" in intro_content.lower()
        body_content = paths["Body"].read_text()
        assert "main thing" in body_content.lower()

    def test_unmatched_goes_to_general(self, notes: PipelineNotes, plan: ArticlePlan) -> None:
        research = ResearchCompilation(
            findings=[
                ResearchFinding(
                    question="Some unrelated question",
                    answer="Answer.",
                    sources=[],
                    confidence=0.5,
                ),
            ],
        )
        paths = split_research_by_section(research, plan, notes)
        assert "_general" in paths

    def test_empty_research(self, notes: PipelineNotes, plan: ArticlePlan) -> None:
        research = ResearchCompilation(findings=[])
        paths = split_research_by_section(research, plan, notes)
        assert paths == {}


class TestBuildVoiceFileRefs:
    def test_empty_list(self) -> None:
        assert build_voice_file_refs([]) == ""

    def test_voice_files(self) -> None:
        refs = build_voice_file_refs(["/tmp/voice_analysis_conversation.md"])
        assert "Voice analysis:" in refs
        assert "/tmp/voice_analysis_conversation.md" in refs

    def test_corpus_files(self) -> None:
        refs = build_voice_file_refs(["/tmp/style_reference_humanizer.md"])
        assert "Style reference:" in refs

    def test_prescriptive_files(self) -> None:
        refs = build_voice_file_refs(["/tmp/prescriptive_rules_guide.md"])
        assert "Prescriptive rules" in refs
        assert "hard constraints" in refs

    def test_mixed_files(self) -> None:
        refs = build_voice_file_refs([
            "/tmp/voice_analysis_conversation.md",
            "/tmp/style_reference_skill.md",
            "/tmp/prescriptive_rules_guide.md",
        ])
        assert "Voice analysis:" in refs
        assert "Style reference:" in refs
        assert "Prescriptive rules" in refs
