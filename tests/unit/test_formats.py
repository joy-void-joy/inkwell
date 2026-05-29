# claude: ignore
"""Tests for format adapter tools.

Tests call through the LupMcpTool SDK handler to exercise the full
tool path including validation and serialization.
"""

import json
from typing import Any

import pytest

from inkwell.agent.tools.formats import (
    format_blog,
    format_lesswrong,
    format_twitter,
    split_sentences,
)
from lup.mcp import LupMcpTool


async def call_tool(tool: LupMcpTool, params: dict[str, Any]) -> dict[str, Any]:
    """Call a LupMcpTool's handler and parse the JSON response."""
    result = await tool.sdk_tool.handler(params)
    content = result.get("content", [])
    assert isinstance(content, list) and len(content) > 0
    text = content[0].get("text", "")
    assert isinstance(text, str)
    return json.loads(text)


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
    @pytest.mark.anyio
    async def test_adds_epistemic_status(self) -> None:
        result = await call_tool(
            format_lesswrong,
            {"content": "Some article text.", "epistemic_status": "fairly confident"},
        )
        assert result["content"].startswith("*Epistemic status: fairly confident*")

    @pytest.mark.anyio
    async def test_converts_asides_to_footnotes(self) -> None:
        result = await call_tool(
            format_lesswrong,
            {
                "content": "Main text (aside: a side note) continues here.",
                "epistemic_status": "uncertain",
            },
        )
        assert "[^1]" in result["content"]
        assert "[^1]: a side note" in result["content"]
        assert "(aside:" not in result["content"]

    @pytest.mark.anyio
    async def test_adds_crossrefs(self) -> None:
        result = await call_tool(
            format_lesswrong,
            {
                "content": "Article body.",
                "epistemic_status": "confident",
                "crossrefs": ["Related Post A", "Related Post B"],
            },
        )
        assert "**Related:**" in result["content"]
        assert "- Related Post A" in result["content"]

    @pytest.mark.anyio
    async def test_word_count_populated(self) -> None:
        result = await call_tool(
            format_lesswrong,
            {"content": "One two three four five.", "epistemic_status": "low"},
        )
        assert result["word_count"] > 0

    @pytest.mark.anyio
    async def test_no_crossrefs_no_related_section(self) -> None:
        result = await call_tool(
            format_lesswrong,
            {"content": "Just text.", "epistemic_status": "moderate"},
        )
        assert "**Related:**" not in result["content"]


class TestFormatTwitter:
    @pytest.mark.anyio
    async def test_hook_is_first_tweet(self) -> None:
        result = await call_tool(
            format_twitter,
            {"content": "Some paragraph here.", "hook": "This is the hook!"},
        )
        assert result["tweets"][0].startswith("This is the hook!")

    @pytest.mark.anyio
    async def test_thread_numbering(self) -> None:
        result = await call_tool(
            format_twitter,
            {
                "content": "First paragraph.\n\nSecond paragraph.",
                "hook": "Hook tweet",
            },
        )
        assert result["thread_count"] == len(result["tweets"])
        for i, tweet in enumerate(result["tweets"], 1):
            assert tweet.endswith(f"{i}/{result['thread_count']}")

    @pytest.mark.anyio
    async def test_long_paragraph_split(self) -> None:
        long_para = "This is a sentence. " * 30
        result = await call_tool(
            format_twitter, {"content": long_para, "hook": "Start"}
        )
        for tweet in result["tweets"]:
            text_part = tweet.rsplit("\n\n", 1)[0]
            assert len(text_part) <= 280

    @pytest.mark.anyio
    async def test_empty_content(self) -> None:
        result = await call_tool(format_twitter, {"content": "", "hook": "Just a hook"})
        assert result["thread_count"] >= 1


class TestFormatBlog:
    @pytest.mark.anyio
    async def test_adds_title_heading(self) -> None:
        result = await call_tool(
            format_blog, {"content": "Article body.", "title": "My Post"}
        )
        assert result["content"].startswith("# My Post")

    @pytest.mark.anyio
    async def test_meta_description_length(self) -> None:
        long_text = "This is a very detailed opening. " * 10
        result = await call_tool(format_blog, {"content": long_text, "title": "Test"})
        assert len(result["meta_description"]) <= 163

    @pytest.mark.anyio
    async def test_short_content_meta_is_full_paragraph(self) -> None:
        result = await call_tool(
            format_blog, {"content": "Short opening line.", "title": "Test"}
        )
        assert result["meta_description"] == "Short opening line."

    @pytest.mark.anyio
    async def test_word_count(self) -> None:
        result = await call_tool(
            format_blog, {"content": "One two three.", "title": "Test"}
        )
        assert result["word_count"] > 0
