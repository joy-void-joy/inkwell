"""Tests for TabTracker diff detection and stage-based edit routing."""

import pytest

from inkwell.agent.pipeline import (
    TabEdit,
    TabTracker,
    extract_edit_summary,
    filter_edits_for_stage,
    normalize_gdoc_text,
)


class TestNormalizeGdocText:
    def test_smart_quotes_normalized(self) -> None:
        assert normalize_gdoc_text("it’s a “test”") == normalize_gdoc_text("it's a \"test\"")

    def test_whitespace_collapsed(self) -> None:
        assert normalize_gdoc_text("hello   world\n\nfoo") == normalize_gdoc_text("hello world foo")

    def test_em_dash_normalized(self) -> None:
        assert normalize_gdoc_text("a — b") == normalize_gdoc_text("a -- b")

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
        assert not TabTracker.has_meaningful_diff(
            "it's a \"test\"", "it’s a “test”"
        )

    def test_real_content_change_is_meaningful(self) -> None:
        assert TabTracker.has_meaningful_diff(
            "The original text", "The modified text"
        )

    def test_empty_current_not_meaningful(self) -> None:
        assert not TabTracker.has_meaningful_diff("some content", "   ")


class TestFilterEditsForStage:
    @pytest.fixture()
    def all_edits(self) -> list[TabEdit]:
        return [
            TabEdit(tab="Source", diff="- old\n+ new"),
            TabEdit(tab="Plan", diff="- section A\n+ section B"),
            TabEdit(tab="Voice", diff="- formal\n+ casual"),
            TabEdit(tab="Research", diff="- finding 1\n+ finding 2"),
            TabEdit(tab="Section: Introduction", diff="- old intro\n+ new intro"),
            TabEdit(tab="Section: Methods", diff="- old methods\n+ new methods"),
            TabEdit(tab="Draft", diff="- old draft\n+ new draft"),
        ]

    def test_research_sees_source_and_plan(self, all_edits: list[TabEdit]) -> None:
        result = filter_edits_for_stage("research", all_edits)
        tabs = {e.tab for e in result}
        assert tabs == {"Source", "Plan"}

    def test_write_sees_voice_and_sections(self, all_edits: list[TabEdit]) -> None:
        result = filter_edits_for_stage("write", all_edits)
        tabs = {e.tab for e in result}
        assert "Voice" in tabs
        assert "Section: Introduction" in tabs
        assert "Source" not in tabs

    def test_write_section_filter(self, all_edits: list[TabEdit]) -> None:
        result = filter_edits_for_stage("write", all_edits, section="Introduction")
        tabs = {e.tab for e in result}
        assert "Section: Introduction" in tabs
        assert "Section: Methods" not in tabs

    def test_merge_sees_sections_only(self, all_edits: list[TabEdit]) -> None:
        result = filter_edits_for_stage("merge", all_edits)
        tabs = {e.tab for e in result}
        assert "Section: Introduction" in tabs
        assert "Source" not in tabs
        assert "Voice" not in tabs

    def test_rewrite_sees_sections_and_draft(self, all_edits: list[TabEdit]) -> None:
        result = filter_edits_for_stage("rewrite", all_edits)
        tabs = {e.tab for e in result}
        assert "Section: Introduction" in tabs
        assert "Draft" in tabs
        assert "Source" not in tabs

    def test_refine_sees_source_plan_research(self, all_edits: list[TabEdit]) -> None:
        result = filter_edits_for_stage("refine", all_edits)
        tabs = {e.tab for e in result}
        assert tabs == {"Source", "Plan", "Research"}
