"""Tests for markdown_to_docs — Google Docs request generation."""

from inkwell.agent.markdown_to_docs import (
    clamp_ranges,
    markdown_to_requests,
    split_markdown_batch,
    utf16_len,
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

    bold_reqs = [
        r for r in batch["formatting"] if "updateTextStyle" in r
    ]
    assert len(bold_reqs) == 1

    rng = bold_reqs[0]["updateTextStyle"]["range"]  # type: ignore[index]
    # 3 emoji = 6 UTF-16 code units, then a space = 1
    assert rng["startIndex"] == 1 + 6 + 1  # start_index=1 + 3 emoji (6) + space (1)
    assert rng["endIndex"] == 1 + 6 + 1 + 4  # + "bold" (4)


def test_clamp_ranges_drops_out_of_bounds() -> None:
    requests = [
        {"updateTextStyle": {"range": {"startIndex": 100, "endIndex": 200}, "textStyle": {}}}
    ]
    result = clamp_ranges(requests, segment_end=50)
    assert result == []


def test_clamp_ranges_clamps_end() -> None:
    requests = [
        {"updateTextStyle": {"range": {"startIndex": 10, "endIndex": 200}, "textStyle": {}}}
    ]
    result = clamp_ranges(requests, segment_end=50)
    assert len(result) == 1
    rng = result[0]["updateTextStyle"]["range"]  # type: ignore[index]
    assert rng["endIndex"] == 50
