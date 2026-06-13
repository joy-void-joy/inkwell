# claude: ignore
"""Tests for format adapters."""

from inkwell.agent.tools.formats import (
    FormatBlogInput,
    FormatLesswrongInput,
    do_format_blog,
    do_format_lesswrong,
    number_thread,
    split_sentences,
    split_thread,
)


class TestSplitSentences:
    def test_simple_sentences(self) -> None:
        result = split_sentences("Hello world. This is a test. Done!")
        assert result == ["Hello world.", "This is a test.", "Done!"]

    def test_empty_string(self) -> None:
        assert split_sentences("") == []

    def test_single_sentence(self) -> None:
        assert split_sentences("Just one.") == ["Just one."]

    def test_question_and_exclamation(self) -> None:
        result = split_sentences("Why? Because! That's how.")
        assert len(result) == 3


class TestFormatLesswrong:
    def test_adds_epistemic_status(self) -> None:
        result = do_format_lesswrong(
            FormatLesswrongInput(
                content="Some article text.", epistemic_status="fairly confident"
            )
        )
        assert result.content.startswith("*Epistemic status: fairly confident*")

    def test_converts_asides_to_footnotes(self) -> None:
        result = do_format_lesswrong(
            FormatLesswrongInput(
                content="Main text (aside: a side note) continues here.",
                epistemic_status="uncertain",
            )
        )
        assert "[^1]" in result.content
        assert "[^1]: a side note" in result.content
        assert "(aside:" not in result.content

    def test_adds_crossrefs(self) -> None:
        result = do_format_lesswrong(
            FormatLesswrongInput(
                content="Article body.",
                epistemic_status="confident",
                crossrefs=["Related Post A", "Related Post B"],
            )
        )
        assert "**Related:**" in result.content
        assert "- Related Post A" in result.content

    def test_word_count_populated(self) -> None:
        result = do_format_lesswrong(
            FormatLesswrongInput(
                content="One two three four five.", epistemic_status="low"
            )
        )
        assert result.word_count > 0

    def test_no_crossrefs_no_related_section(self) -> None:
        result = do_format_lesswrong(
            FormatLesswrongInput(content="Just text.", epistemic_status="moderate")
        )
        assert "**Related:**" not in result.content

    def test_no_header_when_status_empty(self) -> None:
        result = do_format_lesswrong(FormatLesswrongInput(content="Body text."))
        assert "Epistemic status" not in result.content
        assert result.content.startswith("Body text.")


class TestSplitThread:
    def test_hook_is_first_tweet(self) -> None:
        tweets = split_thread("Some paragraph here.", hook="This is the hook!")
        assert tweets[0] == "This is the hook!"

    def test_derives_hook_from_first_line_when_empty(self) -> None:
        tweets = split_thread("# Title Line\n\nBody paragraph.", hook="")
        assert tweets[0] == "Title Line"

    def test_long_paragraph_split(self) -> None:
        long_para = "This is a sentence. " * 30
        tweets = split_thread(long_para, hook="Start")
        for tweet in tweets:
            assert len(tweet) <= 260

    def test_empty_content_still_has_hook(self) -> None:
        tweets = split_thread("", hook="Just a hook")
        assert len(tweets) >= 1


class TestNumberThread:
    def test_appends_position_markers(self) -> None:
        numbered = number_thread(["first", "second"])
        assert numbered[0].endswith("1/2")
        assert numbered[1].endswith("2/2")


class TestFormatBlog:
    def test_adds_title_heading(self) -> None:
        result = do_format_blog(
            FormatBlogInput(content="Article body.", title="My Post")
        )
        assert result.content.startswith("# My Post")

    def test_meta_description_length(self) -> None:
        long_text = "This is a very detailed opening. " * 10
        result = do_format_blog(FormatBlogInput(content=long_text, title="Test"))
        assert len(result.meta_description) <= 163

    def test_short_content_meta_is_full_paragraph(self) -> None:
        result = do_format_blog(
            FormatBlogInput(content="Short opening line.", title="Test")
        )
        assert result.meta_description == "Short opening line."

    def test_word_count(self) -> None:
        result = do_format_blog(FormatBlogInput(content="One two three.", title="Test"))
        assert result.word_count > 0
