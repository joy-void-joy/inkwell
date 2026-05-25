"""Tests for TabTracker diff detection and GDoc text normalization."""

from inkwell.agent.pipeline import (
    TabTracker,
    extract_edit_summary,
    normalize_gdoc_text,
)

# Smart quote constants (avoid encoding issues in source)
LEFT_SINGLE = "‘"
RIGHT_SINGLE = "’"
LEFT_DOUBLE = "“"
RIGHT_DOUBLE = "”"
EM_DASH = "—"


class TestNormalizeGdocText:
    def test_smart_quotes_normalized(self) -> None:
        smart = f"it{RIGHT_SINGLE}s a {LEFT_DOUBLE}test{RIGHT_DOUBLE}"
        plain = 'it\'s a "test"'
        assert normalize_gdoc_text(smart) == normalize_gdoc_text(plain)

    def test_whitespace_collapsed(self) -> None:
        assert normalize_gdoc_text("hello   world\n\nfoo") == normalize_gdoc_text("hello world foo")

    def test_em_dash_normalized(self) -> None:
        assert normalize_gdoc_text(f"a {EM_DASH} b") == normalize_gdoc_text("a -- b")

    def test_meaningful_diff_detected(self) -> None:
        assert normalize_gdoc_text("The cat sat") != normalize_gdoc_text("The dog sat")


class TestExtractEditSummary:
    def test_single_line_change(self) -> None:
        original = "line 1\nline 2\nline 3\nline 4"
        current = "line 1\nline 2 modified\nline 3\nline 4"
        summary = extract_edit_summary(original, current)
        assert "line 2 modified" in summary
        assert "line 1" not in summary or summary.count("line 1") <= 1

    def test_insertion(self) -> None:
        original = "line 1\nline 2"
        current = "line 1\nnew line\nline 2"
        summary = extract_edit_summary(original, current)
        assert "new line" in summary

    def test_deletion(self) -> None:
        original = "line 1\nremove me\nline 2"
        current = "line 1\nline 2"
        summary = extract_edit_summary(original, current)
        assert "remove me" in summary

    def test_no_diff_returns_empty(self) -> None:
        text = "same\ncontent"
        assert extract_edit_summary(text, text) == ""

    def test_does_not_include_full_content(self) -> None:
        original = "\n".join(f"line {i}" for i in range(100))
        lines = original.splitlines()
        lines[50] = "CHANGED LINE"
        current = "\n".join(lines)
        summary = extract_edit_summary(original, current)
        assert "CHANGED LINE" in summary
        assert "line 0" not in summary
        assert "line 99" not in summary


class TestHasMeaningfulDiff:
    def test_gdoc_whitespace_not_meaningful(self) -> None:
        assert not TabTracker.has_meaningful_diff(
            "Hello  world", "Hello world"
        )

    def test_gdoc_smart_quotes_not_meaningful(self) -> None:
        plain = 'it\'s a "test"'
        smart = f"it{RIGHT_SINGLE}s a {LEFT_DOUBLE}test{RIGHT_DOUBLE}"
        assert not TabTracker.has_meaningful_diff(plain, smart)

    def test_real_content_change_is_meaningful(self) -> None:
        assert TabTracker.has_meaningful_diff(
            "The original text", "The modified text"
        )

    def test_empty_current_not_meaningful(self) -> None:
        assert not TabTracker.has_meaningful_diff("some content", "   ")
