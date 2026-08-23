"""What a part of a work carries that a rewrite must not silently lose.

Figures and citations are the two things an author notices missing and a
reviewer does not. A claim that disappeared reads as a shorter section; a
figure that disappeared reads as a section that never had one, and the image
file stays on disk pointing at nothing. Neither shows up in a diff anybody
reads, because the diff of a rewritten subsection is the whole subsection.

**Counted rather than asked about.** Whether Figure 2.13 survived is a
question about two strings, and answering it by reading them is both cheaper
and more reliable than any reviewer. That is what makes the inheritance pass
checkable: it states what it dropped, and this says whether the statement is
true. A pass whose audit were merely believed would be a pass that reports a
clean inheritance for a draft that lost three figures.

**Read through the parsers, both of them.** A figure in this corpus is an HTML
block holding markdown — ``<figure markdown="span">`` around an ``![](…)`` and
a ``<figcaption>`` — which is one format nested in the other, so it takes the
markdown parser to find the block, an HTML parser to take the block apart, and
the markdown parser again for the image and the caption's citation inside it.
"""

from collections.abc import Iterable, Iterator

from bs4 import BeautifulSoup, Tag
from markdown_it.token import Token
from pydantic import BaseModel, ConfigDict, Field

from inkwell.manuscript.ingest import MARKDOWN

HTML_PARSER = "html.parser"
"""Which backend takes a figure block apart.

The standard-library one rather than lxml: these are hand-written blocks a few
lines long inside somebody's markdown, so the tolerant parser buys nothing the
strict one does not and the strict one is a build dependency.
"""

FIGURE_LABEL = "b"
"""What a caption's number is emphasised with — ``<b>Figure 2.12:</b>``.

Read off the element rather than found in the caption's text, so a caption that
mentions another figure by name in its prose does not rename this one.
"""


class Figure(BaseModel):
    """One figure a part carries, and everything a rewrite has to reproduce.

    ``block`` is the whole thing verbatim. A writer handed the number and the
    image path would compose a figure of its own around them, and the result is
    a book whose figures are laid out one way in the chapters nobody revised
    and another way in the chapters somebody did.
    """

    model_config = ConfigDict(frozen=True)

    number: str = Field(
        default="",
        description="How the book numbers it — 'Figure 2.12'. Empty for an "
        "image the author never numbered",
    )
    image: str = Field(description="Path to the image file, as the source spells it")
    caption: str = Field(default="", description="What the caption says")
    block: str = Field(description="The figure's whole source, to be reproduced as is")

    def render(self) -> str:
        """This figure as a constraint a writer is handed."""
        named = self.number or self.image
        return f"{named} — {self.caption}" if self.caption else named


def tokens_in(text: str) -> Iterator[Token]:
    """Every token of a parsed document, inline children included.

    Flattened because nothing here cares about nesting: an image is an image
    whether it sits in a paragraph or in a table cell, and a walk that stopped
    at the block level would miss every one of them.
    """

    def walk(held: Iterable[Token]) -> Iterator[Token]:
        """Each token, then whatever it holds."""
        for token in held:
            yield token
            if token.children:
                yield from walk(token.children)

    yield from walk(MARKDOWN.parse(text))


def attribute(token: Token, name: str) -> str:
    """One attribute of a token, as the string a path or a URL is.

    The parser types an attribute as whatever HTML allows, which is a number
    for a width and a string for everything this module reads. Narrowed once
    here rather than at each caller, none of which has anything to do with the
    numeric case.
    """
    held = token.attrGet(name)
    return held if isinstance(held, str) else ""


def sourced(tokens: Iterable[Token]) -> Iterator[str]:
    """Every image path a token stream points at."""
    for token in tokens:
        if token.type == "image":
            yield attribute(token, "src")


def linked(tokens: Iterable[Token]) -> Iterator[str]:
    """Every URL a token stream links to."""
    for token in tokens:
        if token.type == "link_open":
            yield attribute(token, "href")


def markdown_within(html: str) -> tuple[Token, ...]:
    """The markdown inside an HTML block, parsed.

    ``markdown="span"`` on a figure is mkdocs saying the block's contents are
    markdown, and they are: the image and the caption's citation are both
    written in it. To the outer parser that is opaque HTML, so a reader that
    stopped there would find no image in a figure and no citation in a caption.
    """
    return tuple(tokens_in(BeautifulSoup(html, HTML_PARSER).get_text()))


def html_blocks(text: str) -> Iterator[str]:
    """Each raw HTML block of a document, in order."""
    for token in tokens_in(text):
        if token.type in ("html_block", "html_inline"):
            yield token.content


def numbered(caption: Tag | None) -> str:
    """What the book calls this figure, read off its caption's own emphasis."""
    if caption is None:
        return ""
    label = caption.find(FIGURE_LABEL)
    return label.get_text().strip().removesuffix(":") if label is not None else ""


def figures_in(text: str) -> tuple[Figure, ...]:
    """Every figure a part carries, in the order a reader meets them.

    An image the author wrapped in nothing counts too, and carries no number.
    Dropping it loses exactly as much as dropping a numbered one, and a part
    whose only illustration was a bare ``![](…)`` is precisely the part where
    nobody would notice.
    """

    def wrapped() -> Iterator[Figure]:
        """Each ``<figure>`` block, taken apart through both parsers."""
        for block in html_blocks(text):
            for element in BeautifulSoup(block, HTML_PARSER).find_all("figure"):
                found = element.find("figcaption")
                caption = found if isinstance(found, Tag) else None
                for image in sourced(markdown_within(str(element))):
                    yield Figure(
                        number=numbered(caption),
                        image=image,
                        caption=caption.get_text().strip() if caption else "",
                        block=str(element),
                    )

    def bare() -> Iterator[Figure]:
        """Each image the author wrapped in nothing at all."""
        for image in sourced(tokens_in(text)):
            yield Figure(image=image, block=f"![]({image})")

    held = [*wrapped(), *bare()]
    return tuple({figure.image: figure for figure in held}.values())


def cited_in(text: str) -> tuple[str, ...]:
    """Every URL a part cites, each once, in the order it first appears.

    Caption citations included, which is why the HTML blocks are re-parsed
    rather than skipped: an arXiv link inside a ``<figcaption>`` is a citation
    the same as one in a paragraph, and it is the one a rewrite loses by
    reproducing the figure from a description of it.
    """
    inside = (held for block in html_blocks(text) for held in markdown_within(block))
    found = [*linked(tokens_in(text)), *linked(inside)]
    return tuple(dict.fromkeys(url for url in found if url))


class PartInventory(BaseModel):
    """What one part's text carries, counted rather than judged.

    The pair of these — the standing text's and the rewrite's — is what turns
    "nothing was lost" from an assurance into a subtraction.
    """

    model_config = ConfigDict(frozen=True)

    figures: tuple[Figure, ...] = Field(
        default=(), description="Every figure, in reading order"
    )
    citations: tuple[str, ...] = Field(
        default=(), description="Every URL cited, each once"
    )
    words: int = Field(default=0, description="How long the part runs")

    def images(self) -> tuple[str, ...]:
        """The image path of each figure, which is what identifies one."""
        return tuple(figure.image for figure in self.figures)

    def lost_to(self, later: "PartInventory") -> "PartInventory":
        """What this part carries that ``later`` does not.

        Word count travels as the shortfall rather than as the later draft's
        own length, because a draft that grew has lost no words and the number
        somebody wants is how much is missing.
        """
        kept = later.images()
        return PartInventory(
            figures=tuple(one for one in self.figures if one.image not in kept),
            citations=tuple(
                one for one in self.citations if one not in later.citations
            ),
            words=max(0, self.words - later.words),
        )

    def empty(self) -> bool:
        """Whether this inventory names nothing at all."""
        return not self.figures and not self.citations

    def render(self) -> str:
        """This inventory as a list something can be asked to account for."""

        def lines() -> Iterator[str]:
            """One line per thing carried, figures before citations."""
            for figure in self.figures:
                yield f"- figure: {figure.render()} ({figure.image})"
            for url in self.citations:
                yield f"- citation: {url}"

        return "\n".join(lines())


def inventory_of(text: str) -> PartInventory:
    """Everything one part's text carries that a rewrite could silently drop."""
    return PartInventory(
        figures=figures_in(text),
        citations=cited_in(text),
        words=len(text.split()),
    )
