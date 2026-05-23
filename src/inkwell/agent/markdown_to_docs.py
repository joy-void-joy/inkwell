"""Convert markdown text to Google Docs API BatchUpdate requests.

Parses markdown using markdown_it, builds a flat text string with tracked
formatting ranges, and emits insertText + style update requests that can
be passed directly to documents().batchUpdate().
"""

from typing import TypedDict

from markdown_it import MarkdownIt
from markdown_it.token import Token


class TextRange(TypedDict):
    startIndex: int
    endIndex: int


class TextRangeWithTab(TypedDict):
    startIndex: int
    endIndex: int
    tabId: str


class LocationWithTab(TypedDict):
    index: int
    tabId: str


class LocationNoTab(TypedDict):
    index: int


class BoldSpan(TypedDict):
    start: int
    end: int


class ItalicSpan(TypedDict):
    start: int
    end: int


class LinkSpan(TypedDict):
    start: int
    end: int
    url: str


class HeadingRange(TypedDict):
    start: int
    end: int
    level: int


class BulletRange(TypedDict):
    start: int
    end: int
    ordered: bool


def make_range(
    start: int, end: int, tab_id: str | None
) -> TextRange | TextRangeWithTab:
    if tab_id is not None:
        return TextRangeWithTab(startIndex=start, endIndex=end, tabId=tab_id)
    return TextRange(startIndex=start, endIndex=end)


def make_location(index: int, tab_id: str | None) -> LocationNoTab | LocationWithTab:
    if tab_id is not None:
        return LocationWithTab(index=index, tabId=tab_id)
    return LocationNoTab(index=index)


HEADING_STYLES: dict[int, str] = {
    1: "HEADING_1",
    2: "HEADING_2",
    3: "HEADING_3",
}


def walk_inline(
    children: list[Token],
    text_parts: list[str],
    offset: int,
    bold_spans: list[BoldSpan],
    italic_spans: list[ItalicSpan],
    link_spans: list[LinkSpan],
) -> int:
    """Walk inline token children, appending text and tracking format spans.

    Returns the new offset after all children have been processed.
    """
    pos = offset
    bold_start: int | None = None
    italic_start: int | None = None
    link_start: int | None = None
    link_url: str = ""

    for child in children:
        match child.type:
            case "text" | "code_inline":
                text_parts.append(child.content)
                pos += len(child.content)
            case "softbreak":
                text_parts.append("\n")
                pos += 1
            case "hardbreak":
                text_parts.append("\n")
                pos += 1
            case "strong_open":
                bold_start = pos
            case "strong_close":
                if bold_start is not None:
                    bold_spans.append(BoldSpan(start=bold_start, end=pos))
                    bold_start = None
            case "em_open":
                italic_start = pos
            case "em_close":
                if italic_start is not None:
                    italic_spans.append(ItalicSpan(start=italic_start, end=pos))
                    italic_start = None
            case "link_open":
                link_start = pos
                href = child.attrGet("href")
                link_url = str(href) if href is not None else ""
            case "link_close":
                if link_start is not None:
                    link_spans.append(LinkSpan(start=link_start, end=pos, url=link_url))
                    link_start = None
                    link_url = ""

    return pos


def markdown_to_requests(
    markdown: str, tab_id: str | None = None, start_index: int = 1
) -> list[dict[str, object]]:
    """Convert markdown to Google Docs BatchUpdate requests.

    Returns requests in the order Google Docs API expects: insertText first
    (for the full content block), then paragraph style updates, text style
    updates, and finally bullet formatting.

    Args:
        markdown: Markdown-formatted text to convert.
        tab_id: Google Docs tab ID. When provided, all location/range
            references include it.
        start_index: Document index where content is inserted (default 1,
            the start of an empty document body).
    """
    md = MarkdownIt()
    tokens = md.parse(markdown)

    text_parts: list[str] = []
    offset = 0

    bold_spans: list[BoldSpan] = []
    italic_spans: list[ItalicSpan] = []
    link_spans: list[LinkSpan] = []
    heading_ranges: list[HeadingRange] = []
    bullet_ranges: list[BulletRange] = []

    in_heading: int | None = None
    heading_start: int = 0
    in_bullet_list: bool = False
    in_ordered_list: bool = False
    list_item_start: int = 0
    list_item_starts: list[tuple[int, bool]] = []

    i = 0
    while i < len(tokens):
        token = tokens[i]

        match token.type:
            case "heading_open":
                tag_level = int(token.tag[1:])
                in_heading = tag_level
                heading_start = offset

            case "heading_close":
                if in_heading is not None:
                    text_parts.append("\n")
                    offset += 1
                    heading_ranges.append(
                        HeadingRange(start=heading_start, end=offset, level=in_heading)
                    )
                    in_heading = None

            case "paragraph_open":
                pass

            case "paragraph_close":
                if not in_heading:
                    text_parts.append("\n")
                    offset += 1

            case "inline":
                if token.children:
                    offset = walk_inline(
                        token.children,
                        text_parts,
                        offset,
                        bold_spans,
                        italic_spans,
                        link_spans,
                    )

            case "bullet_list_open":
                in_bullet_list = True

            case "bullet_list_close":
                in_bullet_list = False

            case "ordered_list_open":
                in_ordered_list = True

            case "ordered_list_close":
                in_ordered_list = False

            case "list_item_open":
                list_item_start = offset
                list_item_starts.append(
                    (list_item_start, in_ordered_list and not in_bullet_list)
                )

            case "list_item_close":
                if list_item_starts:
                    item_start, ordered = list_item_starts.pop()
                    bullet_ranges.append(
                        BulletRange(start=item_start, end=offset, ordered=ordered)
                    )

            case "fence":
                text_parts.append(token.content)
                offset += len(token.content)
                if not token.content.endswith("\n"):
                    text_parts.append("\n")
                    offset += 1

            case "code_block":
                text_parts.append(token.content)
                offset += len(token.content)
                if not token.content.endswith("\n"):
                    text_parts.append("\n")
                    offset += 1

            case "hr":
                separator = "---\n"
                text_parts.append(separator)
                offset += len(separator)

        i += 1

    full_text = "".join(text_parts)
    if not full_text:
        return []

    requests: list[dict[str, object]] = []

    requests.append(
        {
            "insertText": {
                "text": full_text,
                "location": make_location(start_index, tab_id),
            }
        }
    )

    for hr in heading_ranges:
        style_name = HEADING_STYLES.get(hr["level"], "HEADING_3")
        requests.append(
            {
                "updateParagraphStyle": {
                    "paragraphStyle": {"namedStyleType": style_name},
                    "range": make_range(
                        hr["start"] + start_index,
                        hr["end"] + start_index,
                        tab_id,
                    ),
                    "fields": "namedStyleType",
                }
            }
        )

    for span in bold_spans:
        requests.append(
            {
                "updateTextStyle": {
                    "textStyle": {"bold": True},
                    "range": make_range(
                        span["start"] + start_index,
                        span["end"] + start_index,
                        tab_id,
                    ),
                    "fields": "bold",
                }
            }
        )

    for span in italic_spans:
        requests.append(
            {
                "updateTextStyle": {
                    "textStyle": {"italic": True},
                    "range": make_range(
                        span["start"] + start_index,
                        span["end"] + start_index,
                        tab_id,
                    ),
                    "fields": "italic",
                }
            }
        )

    for span in link_spans:
        requests.append(
            {
                "updateTextStyle": {
                    "textStyle": {"link": {"url": span["url"]}},
                    "range": make_range(
                        span["start"] + start_index,
                        span["end"] + start_index,
                        tab_id,
                    ),
                    "fields": "link",
                }
            }
        )

    for br in bullet_ranges:
        preset = (
            "NUMBERED_DECIMAL_ALPHA_ROMAN"
            if br["ordered"]
            else "BULLET_DISC_CIRCLE_SQUARE"
        )
        requests.append(
            {
                "createParagraphBullets": {
                    "range": make_range(
                        br["start"] + start_index,
                        br["end"] + start_index,
                        tab_id,
                    ),
                    "bulletPreset": preset,
                }
            }
        )

    return requests


def clear_tab_request(end_index: int, tab_id: str | None = None) -> dict[str, object]:
    """Request to delete all content from a tab (index 1 to end_index)."""
    return {
        "deleteContentRange": {
            "range": make_range(1, end_index, tab_id),
        }
    }
