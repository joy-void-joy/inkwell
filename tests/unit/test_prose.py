# claude: ignore
"""Tests for reading a draft as the structure its author wrote."""

from inkwell.agent.prose import markdown_blocks, spread
from inkwell.agent.segmenter import reader


class TestMarkdownBlocks:
    def test_heading_is_a_heading_not_prose(self) -> None:
        blocks = markdown_blocks("# Title\n\nBody text.")
        assert [block.is_heading for block in blocks] == [True, False]

    def test_hash_inside_a_fence_is_not_a_heading(self) -> None:
        blocks = markdown_blocks("```\n# not a heading\n```\n\nBody.")
        assert not any(block.is_heading for block in blocks)

    def test_plain_drops_inline_markers(self) -> None:
        block = markdown_blocks("A **bold** claim and a [link](http://x).")[0]
        assert block.plain == "A bold claim and a link."
        assert "**" in block.text

    def test_bold_opening_survives_the_empty_leading_text_token(self) -> None:
        """markdown-it opens an inline run with an empty text token."""
        block = markdown_blocks("**The claim.** The support follows.")[0]
        assert block.bold_opening == "The claim."

    def test_no_bold_opening_when_bold_comes_later(self) -> None:
        block = markdown_blocks("The claim is **bold** midway.")[0]
        assert block.bold_opening == ""


class TestBoldedSummaryDetection:
    def test_a_bolded_first_sentence_counts(self) -> None:
        prose = reader().read("**The claim holds.** The support follows here.")
        assert prose.paragraphs[0].summary_is_bolded

    def test_a_bolded_fragment_does_not_count(self) -> None:
        """The convention asks for a claim a reader can follow, not a bold word."""
        prose = reader().read("**Three** reasons follow from this.")
        assert not prose.paragraphs[0].summary_is_bolded


class TestSections:
    def test_every_heading_opens_a_section(self) -> None:
        prose = reader().read("## One\n\nA.\n\n## Two\n\nB.\n\nC.")
        assert [s.heading for s in prose.sections] == ["One", "Two"]
        assert [len(s.paragraphs) for s in prose.sections] == [1, 2]

    def test_prose_before_the_first_heading_gets_a_section(self) -> None:
        prose = reader().read("Opening.\n\n## One\n\nA.")
        assert [s.heading for s in prose.sections] == ["", "One"]

    def test_empty_content_has_no_sections(self) -> None:
        assert reader().read("").sections == []

    def test_headings_are_not_counted_as_paragraphs(self) -> None:
        prose = reader().read("## One\n\nA sentence.")
        assert len(prose.paragraphs) == 1


class TestSpread:
    def test_one_length_has_no_spread(self) -> None:
        assert spread([7]) == 0.0

    def test_no_lengths_has_no_spread(self) -> None:
        assert spread([]) == 0.0

    def test_uniform_lengths_have_no_spread(self) -> None:
        assert spread([5, 5, 5]) == 0.0

    def test_varied_lengths_have_spread(self) -> None:
        assert spread([2, 20]) > 0.0
