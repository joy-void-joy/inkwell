"""Tests for fetch_and_extract focused extraction tool."""

from unittest.mock import AsyncMock, patch

import pytest

from lup.workspace.content_safety import SavedContent
from inkwell.agent.tools.research.fetch import (
    EXTRACT_THRESHOLD_WORDS,
    FetchAndExtractOutput,
    FetchSourceOutput,
    FocusedExtract,
    do_fetch_and_extract,
)


def make_saved(text: str, tmp_path_factory: pytest.TempPathFactory) -> SavedContent:
    """Write text to a temp file and return SavedContent."""
    p = tmp_path_factory.mktemp("content")
    f = p / "test.md"
    f.write_text(text, encoding="utf-8")
    return SavedContent(
        path=str(f),
        word_count=len(text.split()),
        char_count=len(text),
        preview=text[:500],
    )


@pytest.fixture
def short_content(tmp_path_factory: pytest.TempPathFactory) -> FetchSourceOutput:
    text = "Short article about climate change. Only 10 words here."
    return FetchSourceOutput(
        url="https://example.com/short",
        format="text",
        title="Short Article",
        content=make_saved(text, tmp_path_factory),
    )


@pytest.fixture
def long_content(tmp_path_factory: pytest.TempPathFactory) -> FetchSourceOutput:
    text = "word " * (EXTRACT_THRESHOLD_WORDS + 500)
    return FetchSourceOutput(
        url="https://example.com/long",
        format="text",
        title="Long Article",
        content=make_saved(text, tmp_path_factory),
    )


@pytest.fixture
def pdf_content(tmp_path_factory: pytest.TempPathFactory) -> FetchSourceOutput:
    note = "PDF downloaded to /tmp/abc.pdf. Use Read to read it."
    return FetchSourceOutput(
        url="https://example.com/paper.pdf",
        format="pdf",
        title="",
        content=make_saved(note, tmp_path_factory),
        pdf_path="/tmp/abc.pdf",
    )


@pytest.mark.asyncio
async def test_short_content_passes_through(short_content: FetchSourceOutput) -> None:
    with patch(
        "inkwell.agent.tools.research.fetch.do_fetch_source",
        new_callable=AsyncMock,
        return_value=short_content,
    ):
        result = await do_fetch_and_extract(
            "https://example.com/short", "What does it say about climate?"
        )

    assert isinstance(result, FetchAndExtractOutput)
    assert "Short article" in result.extract
    assert result.was_truncated is False
    assert result.title == "Short Article"


@pytest.mark.asyncio
async def test_pdf_passes_through(pdf_content: FetchSourceOutput) -> None:
    with patch(
        "inkwell.agent.tools.research.fetch.do_fetch_source",
        new_callable=AsyncMock,
        return_value=pdf_content,
    ):
        result = await do_fetch_and_extract(
            "https://example.com/paper.pdf", "What are the findings?"
        )

    assert result.was_truncated is False
    assert "PDF downloaded" in result.extract


@pytest.mark.asyncio
async def test_long_content_triggers_extraction(
    long_content: FetchSourceOutput,
) -> None:
    extracted = FocusedExtract(extract="The relevant part about topic X.")

    with (
        patch(
            "inkwell.agent.tools.research.fetch.do_fetch_source",
            new_callable=AsyncMock,
            return_value=long_content,
        ),
        patch(
            "inkwell.agent.client.query",
            new_callable=AsyncMock,
            return_value=extracted,
        ) as mock_query,
    ):
        result = await do_fetch_and_extract(
            "https://example.com/long", "What about topic X?"
        )

    assert result.was_truncated is True
    assert result.extract == "The relevant part about topic X."
    mock_query.assert_called_once()
    call_kwargs = mock_query.call_args
    assert "Focus: What about topic X?" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_fallback_on_query_failure(long_content: FetchSourceOutput) -> None:
    with (
        patch(
            "inkwell.agent.tools.research.fetch.do_fetch_source",
            new_callable=AsyncMock,
            return_value=long_content,
        ),
        patch(
            "inkwell.agent.client.query",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await do_fetch_and_extract(
            "https://example.com/long", "What about topic X?"
        )

    assert result.was_truncated is False
    assert result.word_count == long_content.content.word_count
