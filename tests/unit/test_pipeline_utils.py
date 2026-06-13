"""Tests for pipeline utility functions: slugify, voice refs."""

from inkwell.agent.content import ContentManifest
from inkwell.agent.pipeline import (
    add_voice_refs,
    slugify,
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
