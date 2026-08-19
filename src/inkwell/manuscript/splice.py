"""The text one part of a work holds, and how a rewrite replaces it.

A section file holds several subsections, and the unit anybody revises is the
subsection. So two things have to be exact: which lines of the file are this
part, and how new text takes their place without disturbing a neighbour.

**Replacement is a splice, not a reassembly.** A rewrite of one subsection
leaves its siblings byte-identical, because it never re-emits them. That is
worth insisting on: a model asked to reassemble the whole file would re-emit
prose nobody asked it to touch, and every pass would cost the file's length
and drift its voice a little further from what the author wrote. Over two
hundred parts, revised repeatedly, that is the difference between a loop that
converges on the author's book and one that walks away from it.

An agentic merge still has a place — a subsection whose opening or closing
moved can leave the transition around it wrong — but that is a *section* going
out of date, reached through the same dependency graph as anything else, and
not the price of every edit below it.

**Spans come from the parser.** Line ranges are read off the parsed tokens
rather than found by scanning for ``##``, so a heading inside a fenced code
block is not mistaken for the start of a part, exactly as the import already
refuses to read one as a part of the work.
"""

from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict, Field

from inkwell.manuscript.ingest import MARKDOWN, SUBSECTION_LEVEL, titled
from inkwell.manuscript.tree import ManuscriptNode

LINE_BREAK = "\n"
"""What a line ends with, spelled once because a splice rejoins on it."""


class PartNotFound(Exception):
    """A part named for replacement that the file it should be in does not hold.

    Its own exception because the two ways of arriving here want different
    answers: a heading the author retitled wants the tree imported again, and
    a heading the author deleted wants the part retired from the work.
    """


class HeadingLost(Exception):
    """A replacement that does not open with the part's own heading.

    Refused rather than written, because the damage is silent and outlives the
    run that did it. Spliced in, prose that dropped its heading leaves the file
    with no part where the tree records one: the next sweep reads the part as
    text the author deleted, its standing goes unrunnable, and the prose itself
    has merged into whichever part precedes it — where a later revision of
    *that* part will rewrite it as its own. Nothing about the file looks wrong
    afterwards, which is why this is the one thing checked before writing.
    """


class PartSpan(BaseModel):
    """Which lines of a file one part occupies, its heading line included.

    Half-open, as a range is: ``start`` is the heading itself and ``end`` is
    the line the next part begins at, so slicing needs no adjustment and a
    part running to the end of the file needs no special case.
    """

    model_config = ConfigDict(frozen=True)

    heading: str = Field(description="The part's heading, as the file spells it")
    title: str = Field(description="The heading with any attribute list removed")
    start: int = Field(ge=0, description="Line the heading is on, counting from zero")
    end: int = Field(
        ge=0, description="Line the next part begins at, or the file's end"
    )

    def text_of(self, lines: list[str]) -> str:
        """What this part holds, given the file split into lines."""
        return LINE_BREAK.join(lines[self.start : self.end])


def spans_in(text: str) -> tuple[PartSpan, ...]:
    """Every subsection of one section file, as the lines each occupies.

    Each part runs from its own heading to the next heading of the same level,
    and the last runs to the end of the file. Anything before the first
    heading — the H1 naming the section, and whatever introduces it — belongs
    to no subsection and is deliberately not covered here: it is the section's
    own text, and a rewrite of a subsection must not be able to address it.
    """
    tokens = MARKDOWN.parse(text)
    lines = text.splitlines()
    starts = [
        (token.map[0], following.content)
        for token, following in zip(tokens, tokens[1:])
        if token.type == "heading_open"
        and token.tag == SUBSECTION_LEVEL
        and token.map is not None
    ]

    def bounded() -> Iterator[PartSpan]:
        """Each heading paired with where the following one begins."""
        for index, (start, heading) in enumerate(starts):
            following = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
            yield PartSpan(
                heading=heading, title=titled(heading), start=start, end=following
            )

    return tuple(bounded())


def span_of(text: str, heading: str) -> PartSpan | None:
    """The span one part occupies, by the heading it begins at.

    Matched on the title rather than on the raw heading line, so a part whose
    anchor attribute was edited is still the part the tree recorded — the
    attribute list is presentation, and the import already drops it from what
    the part is called.
    """
    wanted = titled(heading)
    return next((span for span in spans_in(text) if span.title == wanted), None)


def held_text(source: str, node: ManuscriptNode) -> str:
    """What one part holds right now, whether it is a file or a span within one.

    The single answer to "what is this part's text", which both the digest and
    every run that revises a part are asked against. A part that owns its file
    holds the file; a part that shares one holds its own span of it; a part
    whose heading is no longer in the file holds nothing, which is a part the
    author deleted and a state that is about to say so.
    """
    if not node.heading:
        return source
    span = span_of(source, node.heading)
    return "" if span is None else span.text_of(source.splitlines())


def opens_with(replacement: str, heading: str) -> bool:
    """Whether a rewrite still begins at the part it is replacing.

    Asked of the parser rather than of the first line, so a rewrite that opens
    with a fenced block quoting a heading is not mistaken for one that kept its
    own. Two things have to hold: the part's heading is the first the
    replacement declares, and nothing but blank lines precedes it — leading
    prose would be spliced in above the heading, where the file gives it to the
    part before rather than to this one.
    """
    spans = spans_in(replacement)
    if not spans or spans[0].title != titled(heading):
        return False
    return not any(line.strip() for line in replacement.splitlines()[: spans[0].start])


def spliced(source: str, heading: str, replacement: str) -> str:
    """One section file with a single part's lines replaced.

    Raises where the heading is not in the file. A splice that silently
    appended, or silently did nothing, would leave a run reporting success
    over a file it never changed — and the rewrite it was carrying would be
    gone with no way to tell it had ever existed.

    Raises too where the *replacement* lost the heading, which is the same
    argument pointed the other way: a rewrite is refused rather than written
    into a shape that reads as an author's deletion. The part keeps the text it
    had, and the run that returned prose it could not place says so.

    Whether the file ends in a newline is preserved, because that is a
    property of the file rather than of the part being replaced, and a splice
    that dropped it would show up as a change in every diff of the work.
    """
    span = span_of(source, heading)
    if span is None:
        raise PartNotFound(f"No part headed {titled(heading)!r} in this file")
    if not opens_with(replacement, heading):
        raise HeadingLost(
            f"This rewrite does not open with the heading {titled(heading)!r}, "
            "so there is nowhere in the file it can be placed as that part"
        )
    lines = source.splitlines()
    rebuilt = LINE_BREAK.join(
        [*lines[: span.start], *replacement.splitlines(), *lines[span.end :]]
    )
    return rebuilt + LINE_BREAK if source.endswith(LINE_BREAK) else rebuilt
