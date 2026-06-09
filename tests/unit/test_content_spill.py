"""Tests for content safety — oversized tool results spilled to files."""

from pathlib import Path

from pydantic import BaseModel

from lup.content_safety import (
    CONTENT_SAFETY_THRESHOLD,
    SavedContent,
    save_content,
    slugify_label,
    spill_field,
    spill_oversized_result,
    split_on_headings,
)


class TestSlugifyLabel:
    def test_url_strips_protocol_and_domain(self) -> None:
        assert (
            slugify_label("https://example.com/my-article") == "example-com-my-article"
        )

    def test_deep_url_keeps_last_segments(self) -> None:
        result = slugify_label("https://example.com/blog/posts/my-great-post")
        assert "my-great-post" in result

    def test_special_chars_collapsed(self) -> None:
        assert (
            slugify_label("Google Doc: AI Safety Memo!") == "google-doc-ai-safety-memo"
        )

    def test_truncates_to_60_chars(self) -> None:
        long_label = "a-very-long-label-" * 10
        assert len(slugify_label(long_label)) <= 60

    def test_empty_string(self) -> None:
        assert slugify_label("") == ""


class TestSpillField:
    def test_writes_file_and_returns_pointer(self, tmp_path: Path) -> None:
        content = "x" * 30_000
        pointer = spill_field("fetch", "https://example.com", content, tmp_path)
        assert "fetch_" in pointer
        assert "words" in pointer
        files = list(tmp_path.glob("fetch_*.md"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == content

    def test_idempotent_for_same_content(self, tmp_path: Path) -> None:
        content = "same content"
        spill_field("fetch", "label", content, tmp_path)
        spill_field("fetch", "label", content, tmp_path)
        files = list(tmp_path.glob("fetch_*.md"))
        assert len(files) == 1

    def test_different_content_same_label_gets_suffix(self, tmp_path: Path) -> None:
        spill_field("fetch", "label", "content v1", tmp_path)
        spill_field("fetch", "label", "content v2", tmp_path)
        files = list(tmp_path.glob("fetch_*.md"))
        assert len(files) == 2

    def test_readable_filename(self, tmp_path: Path) -> None:
        spill_field("fetch", "https://example.com/article", "content", tmp_path)
        files = list(tmp_path.glob("*.md"))
        assert len(files) == 1
        assert "example-com" in files[0].name


class SampleOutput(BaseModel):
    url: str
    title: str
    content: str
    word_count: int


class TestSpillOversizedResult:
    def test_small_result_unchanged(self, tmp_path: Path) -> None:
        result = SampleOutput(url="u", title="t", content="short", word_count=1)
        spilled = spill_oversized_result("test", "label", result, tmp_path)
        assert spilled.content == "short"

    def test_large_content_spilled(self, tmp_path: Path) -> None:
        big = "word " * 50_000
        result = SampleOutput(url="u", title="t", content=big, word_count=50_000)
        spilled = spill_oversized_result("test", "u", result, tmp_path)
        assert len(spilled.content) < CONTENT_SAFETY_THRESHOLD
        assert "written to" in spilled.content
        assert spilled.url == "u"
        assert spilled.title == "t"
        assert spilled.word_count == 50_000

    def test_spill_file_contains_original(self, tmp_path: Path) -> None:
        big = "word " * 50_000
        result = SampleOutput(url="u", title="t", content=big, word_count=50_000)
        spill_oversized_result("test", "u", result, tmp_path)
        files = list(tmp_path.glob("*.md"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big


class TestSaveContent:
    def test_writes_file_and_returns_metadata(self, tmp_path: Path) -> None:
        content = "Hello world, this is test content."
        result = save_content("test", "label", content, tmp_path)
        assert isinstance(result, SavedContent)
        assert Path(result.path).exists()
        assert result.word_count == 6
        assert result.char_count == len(content)
        assert result.preview == content

    def test_preview_truncated_for_large_content(self, tmp_path: Path) -> None:
        content = "x" * 1000
        result = save_content("test", "label", content, tmp_path)
        assert len(result.preview) == 501
        assert result.preview.endswith("…")

    def test_idempotent_for_same_content(self, tmp_path: Path) -> None:
        content = "same content"
        r1 = save_content("test", "label", content, tmp_path)
        r2 = save_content("test", "label", content, tmp_path)
        assert r1.path == r2.path
        files = list(tmp_path.glob("test_*.md"))
        assert len(files) == 1

    def test_different_content_same_label_gets_suffix(self, tmp_path: Path) -> None:
        save_content("test", "label", "v1", tmp_path)
        save_content("test", "label", "v2", tmp_path)
        files = list(tmp_path.glob("test_*.md"))
        assert len(files) == 2


class TestSplitOnHeadings:
    def test_no_headings_single_chunk(self) -> None:
        chunks = split_on_headings("no headings here\njust text")
        assert len(chunks) == 1
        assert chunks[0][0] == "Full content"

    def test_splits_at_headings(self) -> None:
        content = "preamble\n## Section A\ntext a\n## Section B\ntext b"
        chunks = split_on_headings(content)
        headings = [h for h, _t in chunks]
        assert "Preamble" in headings
        assert "Section A" in headings
        assert "Section B" in headings

    def test_preserves_all_content(self) -> None:
        content = "## One\nfirst\n## Two\nsecond"
        chunks = split_on_headings(content)
        reassembled = "\n".join(t for _h, t in chunks)
        assert reassembled == content
