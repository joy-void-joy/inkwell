"""How a chapter's prose points at the book around it, and what resolves it.

A book is written one chapter per run, so a sentence in chapter three may point
at chapter seven before anything has numbered chapter seven and before anyone
has written it. The writer cannot render ``/chapters/07`` it has no ordinal
for, and cannot be made to wait for one, so it writes down the one thing it
does know — the target's key::

    See [the calibration curve](book:foundations) for why this holds.
    Measured in [the error budget](book:measurement/the-error-budget).

That is a markdown link whose destination is a ``book:`` URI, so what a writer
emits is already structured: the destination is a link target markdown's own
parser hands back through :func:`~inkwell.agent.markdown_to_docs.walk_inline`,
not a phrase a later stage would have to read back out of a sentence. The key
is meaningful on its own, which is what lets it survive its target being
written out of order, and both published address forms are reachable — a
chapter on its own, or a section of one.

**Where resolution happens.** Once, where inkwell writes its own deliverable:
``do_write_tab`` builds a :class:`BookReferences` from the book the run is
writing into and hands it to ``markdown_to_requests``, which resolves each
destination as it walks it. Not at the published site's render time — the site
serving ``/chapters/01/03`` is downstream of this pipeline and is not ours to
run — and not at any earlier stage, because a target numbered between the
writing and the delivery would then have been missed.

**What an unresolved one looks like.** Not dropped, not blanked, and not a link
into a page the sentence did not promise. The prose keeps its words and gains a
marker naming what it wanted, and the write raises a Google Doc comment against
that same text. The author is following the document while it is written, so a
plain-prose fallback would be indistinguishable from a deliberate mention: the
marker is findable where they are reading and the comment is answerable where
they answer everything else.

This is intra-book reference and has nothing to do with ``crossrefs`` in
:mod:`inkwell.agent.tools.formats`, which is a list of unrelated posts a
LessWrong submission links out to.
"""

from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

from inkwell.agent.book import BookRecord, BookTarget

LINK_SCHEME = "book"
"""The URI scheme that makes a markdown link a reference into this book.

A scheme rather than a path prefix, because a path is what a resolved
reference *becomes*: ``/chapters/07`` is an address the site serves, and
``book:foundations`` is a key nothing serves and nothing should try to. A
destination under any other scheme is an ordinary link and is left alone.
"""

MARKER_LABEL = "unresolved reference"
"""The one phrase every unresolved reference is marked with in the document.

One phrase, so the author finds all of them by searching for one thing, and so
no call site can invent its own wording for the same situation.
"""


def parse_target(href: str) -> BookTarget | None:
    """The target a link destination names, where it names one at all.

    Read through a URL parser rather than by looking at the characters: a
    destination is a URI, and its own parser is what says where the scheme
    ends and which segments the path has. Anything that is not a ``book:``
    reference, and any reference too deep or too shallow to be one of the two
    address forms, is left exactly as the writer wrote it.
    """
    parsed = urlsplit(href)
    if parsed.scheme != LINK_SCHEME:
        return None
    named = PurePosixPath(unquote(parsed.path))
    if named.is_absolute():
        return None
    match named.parts:
        case (chapter,):
            return BookTarget(chapter=chapter)
        case (chapter, section):
            return BookTarget(chapter=chapter, section=section)
        case _:
            return None


def marker(target: BookTarget) -> str:
    """What an unresolved reference leaves in the prose it was written on."""
    return f" [{MARKER_LABEL}: {target.spelled()}]"


def raised(target: BookTarget) -> str:
    """What the author is told about one unresolved reference, asynchronously.

    Paired with the marker rather than replacing it, and naming the same
    target in the same words, so the two are matched by eye. It says what the
    document now reads like and what would settle it, because an unresolved
    reference is often correct — the chapter it names is simply still to be
    written — and a note the author can neither act on nor dismiss is noise.
    """
    return (
        f"This sentence points at {target.spelled()}, which this book has not "
        f"numbered yet, so it carries the key rather than a link and the text "
        f"reads '{MARKER_LABEL}: {target.spelled()}'. Lay the book out with "
        f"that chapter in it and the next write resolves it on its own. "
        f"Nothing needs doing here if the target is still to be written."
    )


def pointing(record: BookRecord) -> str:
    """How a chapter's writer points at the rest of the book, and at which keys.

    Rendered into the book a chapter run already reads rather than written
    into a stage's prompt, because the useful half of this is the list of keys
    this particular book declared. A prompt could only describe the form, and
    a key it described from memory would resolve to nothing.
    """
    outlined = record.outline.chapters if record.outline is not None else []

    def lines() -> Iterator[str]:
        """The form, then the keys, then what to do about a key with no chapter."""
        yield (
            f"## Pointing at another chapter of this book\n\n"
            f"Write a cross-reference as a markdown link whose destination is "
            f"the target's key under `{LINK_SCHEME}:` — never an ordinal, and "
            f"never a path:\n"
            f"- `[what you are pointing at]({LINK_SCHEME}:foundations)` — the "
            f"chapter itself\n"
            f"- `[what you are pointing at]({LINK_SCHEME}:foundations/the-"
            f"calibration-curve)` — one section of it, named by that section's "
            f"own title\n\n"
            f"The key is resolved to a published link when the document is "
            f"written, so write the reference whether or not its target has "
            f"been written yet: there is nothing to wait for and no number to "
            f"work out. Do not spell a `/chapters/...` path yourself — a link "
            f"you write by hand is one this book cannot check."
        )
        if outlined:
            yield "The keys this book has declared:\n" + "\n".join(
                f"- `{held.key}` — {held.label()}" for held in outlined
            )
        else:
            yield (
                "This book has declared no keys yet, because no book stage has "
                "laid it out."
            )
        yield (
            f"A chapter with no key above has not been laid out. Point at it "
            f"by the key it should be given, spelled as a lowercase hyphenated "
            f"slug. Until the book stage names it, the reference is marked "
            f"'{MARKER_LABEL}' where it stands in the document and raised with "
            f"the author, rather than dropped or pointed somewhere else."
        )

    return "\n\n".join(lines())


class LinkDestination(BaseModel, frozen=True):
    """What the walk does with one link destination once the book has read it.

    Three outcomes in one shape: an ordinary link keeps its URL, a resolved
    reference takes the published one, and an unresolved reference has neither
    — it is not a link at all, and what it leaves behind is the marker.
    """

    url: str = Field(
        default="", description="Where the link points, empty where nothing does"
    )
    marker: str = Field(
        default="", description="What the prose gains instead of a link, where it does"
    )


class UnresolvedReference(BaseModel, frozen=True):
    """One reference the book could not place, and the words it was written on."""

    target: BookTarget = Field(description="What the reference named")
    text: str = Field(description="The prose the reference was written on")


class BookReferences(BaseModel):
    """The book a document's references resolve against, and the ones that missed.

    One object rather than a resolver beside a list, because the two are one
    reading of the document: what is unresolved is exactly the set of
    destinations this book could not give a path to, and deriving it a second
    time would be a second parser to keep in step with the first.

    Built per write. A standalone piece belongs to no book, holds no record,
    and resolves nothing — which is right, since it has no chapters to point
    at.
    """

    record: BookRecord | None = Field(
        default=None, description="The book being written into, where there is one"
    )
    unresolved: list[UnresolvedReference] = Field(
        default=[],
        description="Every reference this document could not place, in reading order",
    )

    def destination(self, href: str, text: str) -> LinkDestination:
        """Where one link destination points, once this book has read it.

        The seam the whole of reconciliation runs through: every ``book:``
        reference in the delivered document is answered here and nowhere else,
        so an unresolved one takes the same form whichever tab it was written
        into.
        """
        target = parse_target(href)
        if target is None:
            return LinkDestination(url=href)
        address = self.record.address(target) if self.record is not None else None
        if address is None:
            self.unresolved.append(UnresolvedReference(target=target, text=text))
            return LinkDestination(marker=marker(target))
        return LinkDestination(url=address.path)
