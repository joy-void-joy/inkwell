# claude: ignore
"""Tests for format adapters."""

from inkwell.agent.tools.formats import (
    FormatBlogInput,
    FormatLesswrongInput,
    FormatTwitterInput,
    do_format_blog,
    do_format_lesswrong,
    do_format_twitter,
    split_sentences,
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


class TestFormatTwitter:
    def test_hook_is_first_tweet(self) -> None:
        result = do_format_twitter(
            FormatTwitterInput(content="Some paragraph here.", hook="This is the hook!")
        )
        assert result.tweets[0].startswith("This is the hook!")

    def test_thread_numbering(self) -> None:
        result = do_format_twitter(
            FormatTwitterInput(
                content="First paragraph.\n\nSecond paragraph.", hook="Hook tweet"
            )
        )
        assert result.thread_count == len(result.tweets)
        for i, tweet in enumerate(result.tweets, 1):
            assert tweet.endswith(f"{i}/{result.thread_count}")

    def test_long_paragraph_split(self) -> None:
        long_para = "This is a sentence. " * 30
        result = do_format_twitter(FormatTwitterInput(content=long_para, hook="Start"))
        for tweet in result.tweets:
            text_part = tweet.rsplit("\n\n", 1)[0]
            assert len(text_part) <= 280

    def test_empty_content(self) -> None:
        result = do_format_twitter(FormatTwitterInput(content="", hook="Just a hook"))
        assert result.thread_count >= 1


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
