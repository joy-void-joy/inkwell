"""Convert markdown text to Google Docs API BatchUpdate requests.

Parses markdown using markdown_it, builds a flat text string with tracked
formatting ranges, and emits insertText + style update requests that can
be passed directly to documents().batchUpdate().
"""

from typing import TypedDict

from markdown_it import MarkdownIt
from markdown_it.token import Token


def utf16_len(text: str) -> int:
    """Length of *text* in UTF-16 code units (how Google Docs counts indices)."""
    return len(text.encode("utf-16-le")) // 2


class LinkStyle(TypedDict):
    url: str


class RequestTextStyle(TypedDict, total=False):
    """The character formatting one style request sets."""

    bold: bool
    italic: bool
    link: LinkStyle


class RequestParagraphStyle(TypedDict):
    namedStyleType: str


class InsertText(TypedDict):
    text: str
    location: "LocationNoTab | LocationWithTab"


class UpdateTextStyle(TypedDict):
    textStyle: RequestTextStyle
    range: "TextRange | TextRangeWithTab"
    fields: str


class UpdateParagraphStyle(TypedDict):
    paragraphStyle: RequestParagraphStyle
    range: "TextRange | TextRangeWithTab"
    fields: str


class CreateParagraphBullets(TypedDict):
    range: "TextRange | TextRangeWithTab"
    bulletPreset: str


class DeleteContentRange(TypedDict):
    range: "TextRange | TextRangeWithTab"


class Magnitude(TypedDict):
    magnitude: int
    unit: str


class ObjectSize(TypedDict):
    width: Magnitude
    height: Magnitude


class InsertInlineImage(TypedDict):
    uri: str
    objectSize: ObjectSize
    location: "LocationNoTab | LocationWithTab"


class NewTabProperties(TypedDict, total=False):
    """How a tab a request creates is named, and where it is nested."""

    title: str
    parentTabId: str


class AddTab(TypedDict):
    """A request creating one tab."""

    tabProperties: NewTabProperties


class DocsRequest(TypedDict, total=False):
    """One entry of a ``documents().batchUpdate`` request list.

    Every key is optional because the API reads exactly one of them per
    request: which key is present is what the request *is*. Naming them here
    is what lets ``clamp_ranges`` and ``split_markdown_batch`` sort requests
    by shape instead of by probing an untyped mapping.
    """

    insertText: InsertText
    updateTextStyle: UpdateTextStyle
    updateParagraphStyle: UpdateParagraphStyle
    createParagraphBullets: CreateParagraphBullets
    deleteContentRange: DeleteContentRange
    insertInlineImage: InsertInlineImage
    addDocumentTab: AddTab
    addTab: AddTab


class BatchUpdateBody(TypedDict):
    """The body of a ``documents().batchUpdate`` call."""

    requests: list[DocsRequest]


class NewDocumentBody(TypedDict):
    """The body of a ``documents().create`` call."""

    title: str


class MarkdownBatch(TypedDict):
    content: list[DocsRequest]
    formatting: list[DocsRequest]


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
                pos += utf16_len(child.content)
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
) -> list[DocsRequest]:
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
                offset += utf16_len(token.content)
                if not token.content.endswith("\n"):
                    text_parts.append("\n")
                    offset += 1

            case "code_block":
                text_parts.append(token.content)
                offset += utf16_len(token.content)
                if not token.content.endswith("\n"):
                    text_parts.append("\n")
                    offset += 1

            case "hr":
                separator = "---\n"
                text_parts.append(separator)
                offset += utf16_len(separator)

        i += 1

    full_text = "".join(text_parts)
    if not full_text:
        return []

    def span_range(start: int, end: int) -> TextRange | TextRangeWithTab:
        """One tracked span, moved into document coordinates."""
        return make_range(start + start_index, end + start_index, tab_id)

    def styled(
        start: int, end: int, style: RequestTextStyle, fields: str
    ) -> DocsRequest:
        """One character-formatting request over a tracked span."""
        return DocsRequest(
            updateTextStyle=UpdateTextStyle(
                textStyle=style, range=span_range(start, end), fields=fields
            )
        )

    return [
        DocsRequest(
            insertText=InsertText(
                text=full_text, location=make_location(start_index, tab_id)
            )
        ),
        *(
            DocsRequest(
                updateParagraphStyle=UpdateParagraphStyle(
                    paragraphStyle=RequestParagraphStyle(
                        # lup: ignore[dict-get] — a heading depth markdown may
                        # nest deeper than Docs names a style for
                        namedStyleType=HEADING_STYLES.get(hr["level"], "HEADING_3")
                    ),
                    range=span_range(hr["start"], hr["end"]),
                    fields="namedStyleType",
                )
            )
            for hr in heading_ranges
        ),
        *(
            styled(span["start"], span["end"], RequestTextStyle(bold=True), "bold")
            for span in bold_spans
        ),
        *(
            styled(span["start"], span["end"], RequestTextStyle(italic=True), "italic")
            for span in italic_spans
        ),
        *(
            styled(
                span["start"],
                span["end"],
                RequestTextStyle(link=LinkStyle(url=span["url"])),
                "link",
            )
            for span in link_spans
        ),
        *(
            DocsRequest(
                createParagraphBullets=CreateParagraphBullets(
                    range=span_range(br["start"], br["end"]),
                    bulletPreset=(
                        "NUMBERED_DECIMAL_ALPHA_ROMAN"
                        if br["ordered"]
                        else "BULLET_DISC_CIRCLE_SQUARE"
                    ),
                )
            )
            for br in bullet_ranges
        ),
    ]


def clamp_ranges(
    requests: list[DocsRequest],
    segment_end: int,
) -> list[DocsRequest]:
    """Clamp all request ranges to fit within the actual segment.

    Google Docs may count characters differently than Python len() (UTF-16
    code units, stripped control characters, etc.), so the document segment
    can be shorter than expected after insertText. This clamps formatting
    ranges to the actual segment end, dropping any that fall entirely outside.
    """

    def clamped(
        span: TextRange | TextRangeWithTab,
    ) -> TextRange | TextRangeWithTab | None:
        """That range, cut to the segment, or nothing if it starts past it."""
        if span["startIndex"] >= segment_end:
            return None
        if span["endIndex"] <= segment_end:
            return span
        if "tabId" in span:
            return TextRangeWithTab(
                startIndex=span["startIndex"],
                endIndex=segment_end,
                tabId=span["tabId"],
            )
        return TextRange(startIndex=span["startIndex"], endIndex=segment_end)

    def clamp(req: DocsRequest) -> DocsRequest | None:
        """One request, clamped, dropped, or passed through untouched.

        Only the three ranged shapes are reachable here; anything else — an
        ``insertText``, say — carries no range to clamp and passes straight
        through, which is what the untyped version meant by "unmatched".
        """
        if "updateTextStyle" in req:
            style = req["updateTextStyle"].copy()
            span = clamped(style["range"])
            if span is None:
                return None
            style["range"] = span
            return DocsRequest(updateTextStyle=style)
        if "updateParagraphStyle" in req:
            paragraph = req["updateParagraphStyle"].copy()
            span = clamped(paragraph["range"])
            if span is None:
                return None
            paragraph["range"] = span
            return DocsRequest(updateParagraphStyle=paragraph)
        if "createParagraphBullets" in req:
            bullets = req["createParagraphBullets"].copy()
            span = clamped(bullets["range"])
            if span is None:
                return None
            bullets["range"] = span
            return DocsRequest(createParagraphBullets=bullets)
        return req

    return [fitted for req in requests if (fitted := clamp(req)) is not None]


def split_markdown_batch(
    requests: list[DocsRequest],
) -> MarkdownBatch:
    """Split markdown_to_requests output into content and formatting batches."""
    return MarkdownBatch(
        content=[req for req in requests if "insertText" in req],
        formatting=[req for req in requests if "insertText" not in req],
    )


def clear_tab_request(end_index: int, tab_id: str | None = None) -> DocsRequest:
    """Request to delete all content from a tab (index 1 to end_index).

    Excludes the final newline character that terminates every segment —
    the Docs API forbids deleting it.
    """
    return DocsRequest(
        deleteContentRange=DeleteContentRange(
            range=make_range(1, end_index - 1, tab_id)
        )
    )
