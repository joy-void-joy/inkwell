"""Tests for the content contract layer."""

from inkwell.agent.content import ContentEnvelope, build_envelope, extract_sections


class TestExtractSections:
    def test_no_headings(self) -> None:
        content = "Just a plain paragraph.\n\nAnother paragraph."
        sections = extract_sections(content)
        assert sections == []

    def test_single_heading(self) -> None:
        content = "# Title\n\nSome text here.\nMore text."
        sections = extract_sections(content)
        assert len(sections) == 1
        assert sections[0].heading == "Title"
        assert sections[0].offset == 0
        assert sections[0].word_count > 0

    def test_multiple_headings(self) -> None:
        content = "# Intro\n\nIntro text.\n\n## Methods\n\nMethod text.\n\n## Results\n\nResult text."
        sections = extract_sections(content)
        assert len(sections) == 3
        assert sections[0].heading == "Intro"
        assert sections[1].heading == "Methods"
        assert sections[2].heading == "Results"

    def test_heading_levels(self) -> None:
        content = "## Section A\n\nText.\n\n### Subsection\n\nMore."
        sections = extract_sections(content)
        assert len(sections) == 2
        assert sections[0].heading == "Section A"
        assert sections[1].heading == "Subsection"

    def test_section_line_counts(self) -> None:
        content = "## First\n\nLine 1.\nLine 2.\n\n## Second\n\nLine 3."
        sections = extract_sections(content)
        assert len(sections) == 2
        assert sections[0].line_count == 5
        assert sections[1].line_count == 3


class TestBuildEnvelope:
    def test_basic_envelope(self) -> None:
        content = "Hello world. This is test content."
        env = build_envelope(
            label="test",
            stage="extract",
            content_type="source",
            path="/tmp/test.md",
            content=content,
        )
        assert env.label == "test"
        assert env.stage == "extract"
        assert env.word_count == 6
        assert env.char_count == len(content)
        assert env.sections == []
        assert env.extra_paths == []

    def test_envelope_with_sections(self) -> None:
        content = "## Intro\n\nIntro text here.\n\n## Body\n\nBody text here."
        env = build_envelope(
            label="article",
            stage="write",
            content_type="draft",
            path="/tmp/article.md",
            content=content,
        )
        assert len(env.sections) == 2
        assert env.sections[0].heading == "Intro"
        assert env.sections[1].heading == "Body"

    def test_envelope_with_extra_paths(self) -> None:
        env = build_envelope(
            label="big-doc",
            stage="tool",
            content_type="spill",
            path="/tmp/part0.md",
            content="Content.",
            extra_paths=["/tmp/part1.md", "/tmp/part2.md"],
        )
        assert env.extra_paths == ["/tmp/part1.md", "/tmp/part2.md"]

    def test_envelope_serialization(self) -> None:
        env = build_envelope(
            label="test",
            stage="extract",
            content_type="source",
            path="/tmp/test.md",
            content="Some content.",
        )
        roundtripped = ContentEnvelope.model_validate_json(env.model_dump_json())
        assert roundtripped == env
