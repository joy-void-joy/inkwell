"""Tests for voice/style corpus operations."""

from pathlib import Path

import pytest

from inkwell.agent.tools.voice import (
    StyleEntry,
    add_style_reference,
    extract_author_text,
    list_style_references,
    load_style_corpus,
)


@pytest.fixture
def style_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary style corpus directory."""
    corpus = tmp_path / "style"
    corpus.mkdir()
    monkeypatch.setattr(
        "inkwell.agent.tools.voice.settings",
        type("S", (), {"style_corpus_path": str(corpus)})(),
    )
    return corpus


class TestLoadStyleCorpus:
    def test_empty_dir(self, style_dir: Path) -> None:
        samples, sources = load_style_corpus()
        assert samples == []
        assert sources == []

    def test_loads_md_files(self, style_dir: Path) -> None:
        (style_dir / "essay.md").write_text("This is my writing style.")
        samples, sources = load_style_corpus()
        assert len(samples) == 1
        assert sources == ["essay.md"]

    def test_loads_txt_files(self, style_dir: Path) -> None:
        (style_dir / "notes.txt").write_text("Some notes here.")
        samples, sources = load_style_corpus()
        assert len(samples) == 1
        assert sources == ["notes.txt"]

    def test_skips_urls_txt(self, style_dir: Path) -> None:
        (style_dir / "urls.txt").write_text("https://example.com\n")
        (style_dir / "real.md").write_text("Real content.")
        samples, sources = load_style_corpus()
        assert "urls.txt" not in sources
        assert "real.md" in sources

    def test_respects_max_samples(self, style_dir: Path) -> None:
        for i in range(10):
            (style_dir / f"sample_{i:02d}.md").write_text(f"Sample {i}")
        samples, _ = load_style_corpus(max_samples=3)
        assert len(samples) == 3

    def test_truncates_long_content(self, style_dir: Path) -> None:
        (style_dir / "long.md").write_text("x" * 5000)
        samples, _ = load_style_corpus()
        assert len(samples[0]) == 2000

    def test_nonexistent_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "inkwell.agent.tools.voice.settings",
            type("S", (), {"style_corpus_path": "/nonexistent/path"})(),
        )
        samples, sources = load_style_corpus()
        assert samples == []
        assert sources == []


class TestExtractAuthorText:
    def test_no_tags_returns_unchanged(self) -> None:
        text = "Just a plain writing sample with no speaker tags."
        assert extract_author_text(text) == text

    def test_extracts_user_blocks_only(self) -> None:
        conversation = (
            "<user>\nHere is my prompt.\n</user>\n\n"
            "<claude>\nHere is Claude's response.\n</claude>\n\n"
            "<user>\nMy follow-up.\n</user>"
        )
        result = extract_author_text(conversation)
        assert "Here is my prompt." in result
        assert "My follow-up." in result
        assert "Claude's response" not in result

    def test_multiple_user_blocks_joined(self) -> None:
        conversation = (
            "<user>\nFirst message.\n</user>\n\n"
            "<claude>\nReply.\n</claude>\n\n"
            "<user>\nSecond message.\n</user>"
        )
        result = extract_author_text(conversation)
        assert "First message." in result
        assert "Second message." in result

    def test_empty_user_block_skipped(self) -> None:
        conversation = "<user>\n  \n</user>\n\n<user>\nReal content.\n</user>"
        result = extract_author_text(conversation)
        assert "Real content." in result
        assert result.strip() == "Real content."


class TestAddStyleReference:
    def test_add_file(self, style_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "my_essay.md"
        source.write_text("My writing sample.")
        msg = add_style_reference(str(source))
        assert "Added" in msg
        assert (style_dir / "my_essay.md").exists()
        assert (style_dir / "my_essay.md").read_text() == "My writing sample."

    def test_add_url(self, style_dir: Path) -> None:
        msg = add_style_reference("https://example.com/post")
        assert "Added URL" in msg
        urls_file = style_dir / "urls.txt"
        assert urls_file.exists()
        assert "https://example.com/post" in urls_file.read_text()

    def test_add_duplicate_url(self, style_dir: Path) -> None:
        add_style_reference("https://example.com/post")
        msg = add_style_reference("https://example.com/post")
        assert "Already in corpus" in msg


class TestListStyleReferences:
    def test_empty_corpus(self, style_dir: Path) -> None:
        assert list_style_references() == []

    def test_lists_files_and_urls(self, style_dir: Path) -> None:
        (style_dir / "essay.md").write_text("Content")
        (style_dir / "urls.txt").write_text("https://example.com\n")

        entries = list_style_references()
        kinds = {e.kind for e in entries}
        assert "file" in kinds
        assert "url" in kinds

    def test_entry_types(self, style_dir: Path) -> None:
        (style_dir / "test.md").write_text("hello")
        entries = list_style_references()
        assert len(entries) == 1
        assert isinstance(entries[0], StyleEntry)
        assert entries[0].kind == "file"
        assert entries[0].name == "test.md"
        assert entries[0].size > 0
