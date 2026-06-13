"""Tests for pipeline utility functions: slugify, voice refs."""

from inkwell.agent.content import ContentManifest
from inkwell.agent.models import ReviewFinding
from inkwell.agent.pipeline import (
    add_voice_refs,
    consolidate_findings,
    resolve_writer_mode,
    slugify,
)
from inkwell.agent.stages import get_format_guidance


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
    def test_coverage_critical_survives_the_suggestion_cap(self) -> None:
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
        result = consolidate_findings([coverage, *suggestions], max_suggestions=15)
        assert any(
            f.reviewer == "coverage" and f.severity == "critical"
            for f in result.findings
        )
        assert result.dropped_suggestions == 5


class TestFormatGuidancePrecedence:
    def test_empty_for_auto_and_unknown(self) -> None:
        assert get_format_guidance("auto") == ""
        assert get_format_guidance("nonexistent") == ""

    def test_guidance_defers_to_voice(self) -> None:
        assert "voice outranks" in get_format_guidance("memo").lower()
        assert "voice outranks" in get_format_guidance("lesswrong").lower()

    def test_memo_no_longer_mandates_bolding_every_sentence(self) -> None:
        guidance = get_format_guidance("memo")
        assert "sparingly" in guidance
