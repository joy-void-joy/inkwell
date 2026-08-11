"""Tests for extract_tab_markdown — reconstructing markdown from GDoc API structures.

The fixtures build the wire JSON the Docs API actually sends and validate it
into ``Tab``, so these cover the parsing as well as the reconstruction: a
field renamed in the schema fails here rather than quietly reading as absent.
"""

from collections.abc import Mapping

from inkwell.agent.tools.google_docs import extract_tab_markdown
from inkwell.agent.tools.google_docs_schema import Tab

type Wire = Mapping[str, object]
"""One object of the JSON the Docs API sends, as a fixture writes it.

Deliberately loose: a fixture's job here is to write what the API really
sends and let ``Tab`` be the thing that says what that means.
"""


def make_tab(paragraphs: list[Wire], lists: Wire | None = None) -> Tab:
    """Build a minimal GDoc tab structure for testing."""
    document_tab: dict[str, object] = {"body": {"content": paragraphs}}
    if lists is not None:
        document_tab["lists"] = lists
    return Tab.model_validate({"documentTab": document_tab})


def make_paragraph(
    runs: list[Wire],
    named_style: str = "NORMAL_TEXT",
    bullet: Wire | None = None,
) -> Wire:
    paragraph: dict[str, object] = {
        "elements": [{"textRun": r} for r in runs],
        "paragraphStyle": {"namedStyleType": named_style},
    }
    if bullet is not None:
        paragraph["bullet"] = bullet
    return {"paragraph": paragraph}


def run(text: str, bold: bool = False, italic: bool = False, url: str = "") -> Wire:
    style: dict[str, object] = {}
    if bold:
        style["bold"] = True
    if italic:
        style["italic"] = True
    if url:
        style["link"] = {"url": url}
    return {"content": text + "\n", "textStyle": style}


class TestPlainText:
    def test_single_paragraph(self) -> None:
        tab = make_tab([make_paragraph([run("Hello world")])])
        assert extract_tab_markdown(tab) == "Hello world\n"

    def test_multiple_paragraphs(self) -> None:
        tab = make_tab(
            [
                make_paragraph([run("First")]),
                make_paragraph([run("Second")]),
            ]
        )
        assert extract_tab_markdown(tab) == "First\n\nSecond\n"


class TestHeadings:
    def test_h1(self) -> None:
        tab = make_tab([make_paragraph([run("Title")], named_style="HEADING_1")])
        assert extract_tab_markdown(tab) == "# Title\n"

    def test_h2(self) -> None:
        tab = make_tab([make_paragraph([run("Section")], named_style="HEADING_2")])
        assert extract_tab_markdown(tab) == "## Section\n"

    def test_h3(self) -> None:
        tab = make_tab([make_paragraph([run("Sub")], named_style="HEADING_3")])
        assert extract_tab_markdown(tab) == "### Sub\n"


class TestInlineFormatting:
    def test_bold(self) -> None:
        tab = make_tab([make_paragraph([run("important", bold=True)])])
        assert extract_tab_markdown(tab) == "**important**\n"

    def test_italic(self) -> None:
        tab = make_tab([make_paragraph([run("emphasis", italic=True)])])
        assert extract_tab_markdown(tab) == "*emphasis*\n"

    def test_bold_italic(self) -> None:
        tab = make_tab([make_paragraph([run("both", bold=True, italic=True)])])
        assert extract_tab_markdown(tab) == "***both***\n"

    def test_link(self) -> None:
        tab = make_tab([make_paragraph([run("click here", url="https://example.com")])])
        assert extract_tab_markdown(tab) == "[click here](https://example.com)\n"

    def test_mixed_runs(self) -> None:
        tab = make_tab(
            [
                make_paragraph(
                    [
                        run("normal "),
                        run("bold", bold=True),
                        run(" end"),
                    ]
                )
            ]
        )
        result = extract_tab_markdown(tab)
        assert "normal " in result
        assert "**bold**" in result
        assert " end" in result


class TestLists:
    def test_unordered_list(self) -> None:
        list_id = "list1"
        lists = {list_id: {"listProperties": {"nestingLevels": [{"glyphType": ""}]}}}
        tab = make_tab(
            [
                make_paragraph(
                    [run("item one")],
                    bullet={"listId": list_id, "nestingLevel": 0},
                ),
                make_paragraph(
                    [run("item two")],
                    bullet={"listId": list_id, "nestingLevel": 0},
                ),
            ],
            lists=lists,
        )
        result = extract_tab_markdown(tab)
        assert "- item one" in result
        assert "- item two" in result

    def test_ordered_list(self) -> None:
        list_id = "list2"
        lists = {
            list_id: {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}
        }
        tab = make_tab(
            [
                make_paragraph(
                    [run("first")],
                    bullet={"listId": list_id, "nestingLevel": 0},
                ),
                make_paragraph(
                    [run("second")],
                    bullet={"listId": list_id, "nestingLevel": 0},
                ),
            ],
            lists=lists,
        )
        result = extract_tab_markdown(tab)
        assert "1. first" in result
        assert "1. second" in result


class TestEdgeCases:
    def test_empty_tab(self) -> None:
        tab = make_tab([])
        assert extract_tab_markdown(tab) == "\n"

    def test_a_tab_the_api_sent_nothing_for(self) -> None:
        """An absent documentTab reads as an empty one, not as a crash.

        This used to be a guard against a non-dict argument; the schema makes
        that unrepresentable, so what is left to check is the response the
        API really can send — one with the content key missing entirely.
        """
        assert extract_tab_markdown(Tab.model_validate({})) == "\n"

    def test_empty_paragraph(self) -> None:
        tab = make_tab([make_paragraph([])])
        result = extract_tab_markdown(tab)
        assert result.strip() == ""

    def test_heading_with_bold_run(self) -> None:
        tab = make_tab(
            [
                make_paragraph(
                    [run("Bold heading", bold=True)], named_style="HEADING_1"
                ),
            ]
        )
        assert extract_tab_markdown(tab) == "# **Bold heading**\n"
