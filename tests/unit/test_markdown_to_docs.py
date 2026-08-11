"""Tests for markdown_to_docs — Google Docs request generation."""

from inkwell.agent.markdown_to_docs import (
    DocsRequest,
    RequestTextStyle,
    TextRange,
    UpdateTextStyle,
    clamp_ranges,
    markdown_to_requests,
    split_markdown_batch,
    utf16_len,
)


def styled(start: int, end: int) -> DocsRequest:
    """One bold-style request over the given range."""
    return DocsRequest(
        updateTextStyle=UpdateTextStyle(
            range=TextRange(startIndex=start, endIndex=end),
            textStyle=RequestTextStyle(bold=True),
            fields="bold",
        )
    )


def test_utf16_len_ascii() -> None:
    assert utf16_len("hello") == 5


def test_utf16_len_emoji() -> None:
    assert utf16_len("\U0001f600") == 2  # 😀 is a surrogate pair


def test_utf16_len_mixed() -> None:
    assert utf16_len("hi \U0001f600 bye") == 9  # "hi "=3, 😀=2, " bye"=4


def test_formatting_indices_account_for_surrogates() -> None:
    """Formatting ranges must use UTF-16 offsets, not Python len()."""
    md = "\U0001f600\U0001f600\U0001f600 **bold**\n"
    requests = markdown_to_requests(md)
    batch = split_markdown_batch(requests)

    bold_reqs = [r for r in batch["formatting"] if "updateTextStyle" in r]
    assert len(bold_reqs) == 1

    rng = bold_reqs[0]["updateTextStyle"]["range"]  # type: ignore[index]
    # 3 emoji = 6 UTF-16 code units, then a space = 1
    assert rng["startIndex"] == 1 + 6 + 1  # start_index=1 + 3 emoji (6) + space (1)
    assert rng["endIndex"] == 1 + 6 + 1 + 4  # + "bold" (4)


def test_clamp_ranges_drops_out_of_bounds() -> None:
    assert clamp_ranges([styled(100, 200)], segment_end=50) == []


def only_style(requests: list[DocsRequest]) -> UpdateTextStyle:
    """The single text-style request in a clamped result."""
    assert len(requests) == 1
    style = requests[0].get("updateTextStyle")
    assert style is not None
    return style


def test_clamp_ranges_clamps_end() -> None:
    style = only_style(clamp_ranges([styled(10, 200)], segment_end=50))
    assert style["range"]["endIndex"] == 50


def test_clamp_ranges_keeps_the_rest_of_the_request() -> None:
    """Clamping rewrites the range and nothing else about the request."""
    style = only_style(clamp_ranges([styled(10, 200)], segment_end=50))
    assert style["fields"] == "bold"
    assert style["textStyle"] == RequestTextStyle(bold=True)
