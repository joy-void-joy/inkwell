"""Which chapter of which book a run is writing, and what the book holds.

A run writes one chapter. Everything a chapter needs from the chapters beside
it — a term an earlier chapter already defined, a source it already leaned on,
what it claimed and where — has to survive the run that produced it, because
the next chapter is a different run and often a different day. So a book is
two things here: an identity a plan carries, and a record on disk keyed by
that identity.

:class:`ChapterPlacement` is the identity, composed onto the plan rather than
spelled across two of its fields, the way :class:`~inkwell.agent.provenance.
SourceProvenance` composes onto a research source. A standalone article holds
none: it is genuinely bookless rather than a book of one chapter, so every
per-run path stays exactly what it was and the book path is additive.

Beside the per-chapter records sits the book's **outline** — its reading order,
the ordinal each chapter holds, and the cross-references between them. That is
what the book stage owns and what every later run reads: a chapter run that had
to re-derive the order from its own source material would renumber the book
from whichever chapter happened to run last.

**An ordinal is an identity, not a position.** Once a chapter has been assigned
one it keeps it, whatever the reading order becomes: an inserted chapter takes
an ordinal the book has never used, and a chapter dropped from the order keeps
its own in :attr:`BookOutline.retired` so nothing is ever handed out twice.
Renumbering would be cheap here and ruinous outside — the reader-feedback export
is keyed by chapter and section ordinals readers have already seen published, so
shifting them re-addresses every row that was already filed.

Because the ordinals are identities, an **address** built from them is stable
too, and both of the things that need one live outside this module: the
reader-feedback export routes a submission by ``chapter.section``, and a
reference in a chapter's prose resolves to ``/chapters/01/03``. So
:class:`ChapterAddress` and :class:`SectionAddress` are declared here, where
the ordinals are, rather than beside either consumer — neither imports the
other for it, and the published path is derived from the same padded ordinals
as the file stem rather than kept in step with it as a second string.

The record lives outside any session, beside the research corpus and for the
same reason — a session's notes die with the run, and a book assembled one
chapter per run would have nothing left to read.

**Concurrency.** One file per chapter, written by the run that owns that
chapter. Two chapter runs of one book therefore never write the same path, and
that partition — not a lock — is the guarantee: :meth:`BookStore.chapter_path`
derives the file from the record's own placement, so a run has no way to name
another chapter's file. The outline is one more single-writer file: the book
stage lays it out and a chapter run only ever reads it. Each write is an atomic
temp-and-rename through ``publish_atomic``, so a reader assembling the book sees
whole records or none, never a half-written one. Two runs of the *same* chapter
do land on one path; there the rename decides, and the later record replaces the
earlier one whole rather than blending with it.

    books/
    └── ai-safety-textbook/
        ├── outline.json # the book's order and cross-references, from the book stage
        ├── 001.json     # one file per chapter, written by that chapter's run
        └── 003.json
"""

import logging
from collections.abc import Iterator, Sequence
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from lup.channels.models import publish_atomic, utc_now

logger = logging.getLogger(__name__)

BOOK_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
"""What a book identity may be spelled with: a lowercase hyphenated slug.

The identity has to be typed the same way by every chapter run of one book,
months apart, and it names a directory. A slug is both — stable enough that
chapter nine reaches chapter one's record, and narrow enough that no book name
can walk out of the store's root. What the book is *called* is prose, and lives
in the record rather than in the key.
"""


def comparable(title: str) -> str:
    """A title reduced to what two spellings of the same one share.

    Whitespace and case are how one title gets typed differently on two days;
    everything else is a difference that means something, so nothing further is
    stripped. Used only to match a title against one already on record — never
    to decide that two chapters are the same chapter.
    """
    return " ".join(title.split()).casefold()


def comparable_key(name: str) -> str:
    """A declared name reduced to what two spellings of it share.

    Beside :func:`comparable` and for the same reason, one step further. A key
    is written into a URL, where a title's spaces become hyphens and its
    punctuation is dropped, so only letters and digits survive on both sides:
    "The Calibration Curve" and ``the-calibration-curve`` are one name. Used
    only to match a name against one already on record.
    """
    return "".join(char for char in name.casefold() if char.isalnum())


ORDINAL_DIGITS = 2
"""How wide an ordinal is written wherever this book is addressed.

Measured rather than chosen: the reader export's own ``pathname`` field
records ``/chapters/01/03``, so two digits is the width readers have already
been given links in. Every address spells its ordinals through :func:`padded`,
so a file stem and a published path cannot drift into two paddings.
"""


def padded(ordinal: int) -> str:
    """One ordinal as every address of this book spells it."""
    return f"{ordinal:0{ORDINAL_DIGITS}d}"


CHAPTERS_ROOT = "/chapters"
"""The published path every chapter of a book hangs under.

Read off the reader export rather than guessed at, and a default rather than a
constant: a second publication that serves the same book somewhere else
overrides this instead of forking the addressing.
"""


class ChapterAddress(BaseModel, frozen=True):
    """Where a chapter sits, as the published book addresses it.

    One of the two forms the reader export records — ``/chapters/01``, the
    chapter's own index page — and what a reference naming a chapter rather
    than a section inside it resolves to.
    """

    chapter: int = Field(description="1-based chapter ordinal")

    def ordinals(self) -> list[str]:
        """This address's ordinals, each spelled the one way."""
        return [padded(self.chapter)]

    @property
    def key(self) -> str:
        """The address as a file stem, zero-padded so a listing sorts."""
        return ".".join(self.ordinals())

    @property
    def path(self) -> str:
        """The published path — the same ordinals under a different separator.

        Derived rather than stored beside :attr:`key`, so the padding readers
        already have links in cannot come to differ between the two.
        """
        return "/".join([CHAPTERS_ROOT, *self.ordinals()])

    def label(self) -> str:
        """The address as a reader of the outline would say it."""
        return str(self.chapter)


class SectionAddress(BaseModel, frozen=True):
    """Where a submission or a plan section sits in the work's ordinal outline.

    The one identity both sides share: the reader-feedback export keys rows by
    chapter and section number, the plan's own sections yield the same path,
    and a reference into the book's prose resolves to it. It lives here rather
    than beside either of those because it is neither's — it is the book's
    ordinals, which this module already holds fixed as identities, and a
    consumer reaching it here reaches it for that reason rather than through a
    module it wanted nothing else from.
    """

    chapter: int = Field(description="1-based chapter ordinal")
    section: int = Field(description="1-based section ordinal within the chapter")

    def ordinals(self) -> list[str]:
        """This address's ordinals, each spelled the one way."""
        return [padded(self.chapter), padded(self.section)]

    @property
    def key(self) -> str:
        """The address as a file stem, zero-padded so a listing sorts."""
        return ".".join(self.ordinals())

    @property
    def path(self) -> str:
        """The published path — the same ordinals under a different separator."""
        return "/".join([CHAPTERS_ROOT, *self.ordinals()])

    def label(self) -> str:
        """The address as a reader of the outline would say it."""
        return f"{self.chapter}.{self.section}"


type BookAddress = ChapterAddress | SectionAddress
"""Somewhere in the published book, at whichever depth it was addressed.

The two forms the reader export measures, and no third: a chapter index and a
section of a chapter. Both answer :attr:`~ChapterAddress.path`, so a caller
rendering a link never asks which one it is holding.
"""


ORDINAL_CHARS = "0123456789.:-—"
"""Digits and the separators an outline number is written with.

A token carrying anything else is a word, so the title opens with prose rather
than with an ordinal.
"""


def ordinal_prefix(title: str) -> SectionAddress | None:
    """The ordinal path a section title opens with, where it opens with one.

    A title carried over from an outline usually keeps its number — "1.3
    Foundation models", "01.03 Foundation models" — and that number is the
    section's own identity, outranking its position in the plan, which shifts
    whenever a section is added or dropped. Read as the digit runs of the
    opening token: exactly two of them is a chapter and a section, and any
    other shape belongs to the prose.
    """
    opening = title.split()
    if not opening:
        return None
    token = opening[0]
    if any(char not in ORDINAL_CHARS for char in token):
        return None
    runs = ["".join(chars) for digit, chars in groupby(token, str.isdigit) if digit]
    if len(runs) != 2:
        return None
    return SectionAddress(chapter=int(runs[0]), section=int(runs[1]))


class PlacedSection(BaseModel, frozen=True):
    """One section of a chapter, and the ordinal it holds — or that it holds none.

    ``section`` absent is a real answer rather than a gap: it says this
    section's chapter gives it no identity, so nothing may address it. Reading
    it as a position instead is what would hand its readers to whichever
    section really does hold that number.
    """

    title: str = Field(description="The section's title, exactly as written")
    name: str = Field(
        description=(
            "That title without the ordinal it opens with — what a reference "
            "spells when it names the section rather than its number"
        )
    )
    declared: SectionAddress | None = Field(
        default=None,
        description="The address the title's own ordinal names, where it carries one",
    )
    section: int | None = Field(
        default=None,
        description="The ordinal this section holds, absent where nothing gives it one",
    )

    def answers(self, spelled: str) -> bool:
        """Whether a reference spelling ``spelled`` names this section.

        Either spelling of the title answers — carrying the ordinal it opens
        with, or not — because a writer copying a numbered heading and a
        writer naming the section are pointing at the same section.
        """
        wanted = comparable_key(spelled)
        return wanted in (comparable_key(self.name), comparable_key(self.title))


def placed_sections(titles: Sequence[str]) -> list[PlacedSection]:
    """Which ordinal each of a chapter's section titles holds.

    The one rule that decides it, because two things read the same list of
    titles: the reader-feedback export routes a submission to a section by it,
    and a reference in another chapter's prose resolves to a section by it.
    Both end at a published address, so the two answering differently means a
    reader lands on a real page the sentence did not promise.

    A title that kept its number is named by that number, which is its
    identity and outranks its place in the list — a list that shifts whenever
    a section is added or dropped. So where *any* title in the chapter carries
    one, the numbered titles hold the ordinals they name and the unnumbered
    ones hold none at all: position inside a chapter whose other sections have
    named themselves is not an identity, and an introduction sitting before
    section 1 would otherwise be handed section 1's readers. Only where no
    title carries a number does position decide, because there it is all there
    is and nothing contradicts it.
    """
    read = [ordinal_prefix(title) for title in titles]
    numbered = any(own is not None for own in read)

    def placed() -> Iterator[PlacedSection]:
        """Each title, with the ordinal this chapter's own spelling gives it."""
        for position, (title, own) in enumerate(zip(titles, read), 1):
            if own is None:
                yield PlacedSection(
                    title=title, name=title, section=None if numbered else position
                )
            else:
                yield PlacedSection(
                    title=title,
                    name=" ".join(title.split()[1:]),
                    declared=own,
                    section=own.section,
                )

    return list(placed())


class BookTarget(BaseModel, frozen=True):
    """What one reference in a chapter's prose names, by key rather than number.

    A key, because the target may have no ordinal yet: chapter three can be
    written on its own and still point at chapter seven, which nothing has
    numbered and nobody has written. The chapter is named by the key its
    book's layout declared, and the section — where a reference names one at
    all — by that section's declared title, matched through
    :func:`comparable_key` however either side happens to be spelled.
    """

    chapter: str = Field(description="Declared key of the chapter referred to")
    section: str = Field(
        default="",
        description=(
            "The section within that chapter, by its declared title, empty "
            "where the reference names the chapter itself"
        ),
    )

    def spelled(self) -> str:
        """This target as a reference writes it: ``chapter`` or ``chapter/section``."""
        return f"{self.chapter}/{self.section}" if self.section else self.chapter


class ChapterPlacement(BaseModel, frozen=True):
    """Which chapter of which book a piece of writing is.

    One object rather than two fields on the plan, so an unplaced piece is one
    absent thing rather than a combination of nulls a reader has to interpret.
    """

    book: str = Field(
        pattern=BOOK_ID_PATTERN,
        description=(
            "The book this chapter belongs to, as a lowercase hyphenated slug. "
            "Every chapter run of one book spells it the same way — it is what "
            "the book's record is addressed by"
        ),
    )
    chapter: int = Field(ge=1, description="1-based chapter ordinal within the book")

    def label(self) -> str:
        """Where this sits, as a reader of the book would say it."""
        return f"{self.book} chapter {self.chapter}"

    def assigned(self) -> "ChapterAssignment":
        """This placement as an assignment — both halves already settled.

        What a run that already knows where it sits hands to the code that
        takes an assignment, so a stage reading the book has one kind of value
        to understand whether the launch named the ordinal or the record did.
        """
        return ChapterAssignment(book=self.book, chapter=self.chapter)


class ChapterAssignment(BaseModel, frozen=True):
    """Which book a run writes, and which chapter of it where the launch said.

    The chapter is optional and the book is not, because only one of the two
    halves can be answered from somewhere else: a book's own record says which
    chapter carries a given title, and nothing anywhere says which book an
    unattached chapter belongs to. A run therefore names its book and may leave
    the ordinal to :meth:`BookRecord.identify`, which is what lets one chapter
    be re-run without the book stage that would have numbered it.
    """

    book: str = Field(
        pattern=BOOK_ID_PATTERN, description="The book this run writes a chapter of"
    )
    chapter: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The chapter ordinal the launch named, absent where it left the "
            "book's own record to say which chapter this is"
        ),
    )

    def declared(self) -> ChapterPlacement | None:
        """The placement the launch itself settled, where it settled one."""
        if self.chapter is None:
            return None
        return ChapterPlacement(book=self.book, chapter=self.chapter)

    def spelled(self) -> str:
        """This assignment as a surface spells it: ``book`` or ``book:chapter``."""
        return self.book if self.chapter is None else f"{self.book}:{self.chapter}"


type IdentitySource = Literal["declared", "recorded", "appended"]
"""How a run came to know which chapter it is.

``declared``: the launch named the ordinal outright. ``recorded``: the book's
own outline or chapter records already place this title. ``appended``: neither
did, so the run took an ordinal the book has never used — the one case where a
run had to assume something, and therefore the one that has to say so.
"""


class ChapterIdentity(BaseModel, frozen=True):
    """Which chapter a run is, and how that was decided.

    Carrying the *how* beside the placement is what lets a run state an
    assumption instead of quietly acting on one: a chapter appended because the
    book had nothing on record reads exactly like a declared one at the point of
    use, and the difference only survives if it is written down here.
    """

    placement: ChapterPlacement = Field(description="Which chapter this run is")
    source: IdentitySource = Field(description="What settled the ordinal")

    def render(self) -> str:
        """What the run knows about its own place, in the terms it decided it."""
        where = self.placement.label()
        match self.source:
            case "declared":
                return f"Writing {where}, as this run was launched."
            case "recorded":
                return (
                    f"Writing {where} — the book's own record already places "
                    f"this title there."
                )
            case "appended":
                return (
                    f"Writing {where}. Assumed: {self.placement.book} records no "
                    f"chapter under this title and no order this one fits into, "
                    f"so this run took the next ordinal the book has never used. "
                    f"Relaunch with --chapter {self.placement.book}:<n> to place "
                    f"it somewhere else, or run the book stage to lay the "
                    f"book's order out first."
                )


type ChapterRelation = Literal["depends_on", "elaborates", "revisits", "contrasts"]
"""What one chapter does to another across a cross-reference.

``depends_on``: the source cannot be read without what the target established.
``elaborates``: the source develops something the target introduced.
``revisits``: the source returns to the target's subject with more to say.
``contrasts``: the source sets itself against the target's case.
"""

RELATION_PHRASING: dict[ChapterRelation, str] = {
    "depends_on": "depends on",
    "elaborates": "elaborates",
    "revisits": "revisits",
    "contrasts": "contrasts with",
}
"""How each relation reads in a sentence a chapter's stage is shown."""


class CrossReference(BaseModel, frozen=True):
    """One chapter leaning on another, and what passes between them.

    Two ordinals and a named relation rather than a sentence, because a later
    stage has to *resolve* this — find the chapter it points at, check it is
    still in the book, render it beside that chapter's title. A sentence would
    have to be parsed back apart to do any of that, and the parse would be a
    guess where this is a lookup.
    """

    from_chapter: int = Field(ge=1, description="Ordinal of the chapter that refers")
    to_chapter: int = Field(ge=1, description="Ordinal of the chapter referred to")
    relation: ChapterRelation = Field(description="What the referring chapter does")
    subject: str = Field(
        description=(
            "What is carried across — the term, result, or claim the referring "
            "chapter needs from the one it names"
        )
    )


class ChapterEntry(BaseModel, frozen=True):
    """One chapter of the book's outline: its identity, its ordinal, its subject.

    ``key`` is what survives a retitling and a reordering, so it is what the
    ordinal is held fixed against. ``ordinal`` is the identity readers and the
    feedback export already have; where the two disagree about position, the
    outline's order is the reading order and the ordinal is only a name.
    """

    key: str = Field(
        pattern=BOOK_ID_PATTERN,
        description=(
            "Stable slug naming this chapter across layouts — what it is "
            "called does not change when its title or its place does"
        ),
    )
    ordinal: int = Field(ge=1, description="The ordinal this chapter holds, for good")
    title: str = Field(description="The chapter's own title")
    thesis: str = Field(default="", description="What the chapter argues, in one line")

    def label(self) -> str:
        """This chapter as a reader of the outline would name it."""
        return f"chapter {self.ordinal} ({self.title})"


def entry_keyed(entries: Sequence[ChapterEntry], key: str) -> ChapterEntry | None:
    """The chapter ``key`` names among ``entries``, however either side spells it.

    *Which* entries to search is the caller's question and deliberately not
    this function's, because two different questions are asked of the same
    match. Where a reference *lands* is asked of the reading order, since a
    chapter dropped from it is a page nothing may be addressed to. Whether a
    target was ever *written* is asked of every ordinal the book has handed
    out, since a dropped chapter was written all the same. Sharing the match
    but not the list is what keeps the two from disagreeing about which
    chapter a key names while still answering different things.
    """
    wanted = comparable_key(key)
    return next((held for held in entries if comparable_key(held.key) == wanted), None)


class ResolvedReference(BaseModel, frozen=True):
    """A cross-reference paired with the two chapters it actually names.

    What "a later stage resolves it" produces: the reference on its own is two
    numbers, and this is those numbers looked up in the outline that assigned
    them, so a stage renders titles rather than ordinals it would have to
    explain.
    """

    reference: CrossReference = Field(description="The recorded reference")
    source: ChapterEntry = Field(description="The chapter that refers")
    target: ChapterEntry = Field(description="The chapter referred to")

    def render(self) -> str:
        """This reference as a sentence, built from the parts rather than stored."""
        relation = RELATION_PHRASING[self.reference.relation]
        return (
            f"{self.source.label()} {relation} {self.target.label()}: "
            f"{self.reference.subject}"
        )


class ProposedChapter(BaseModel, frozen=True):
    """One chapter as a layout proposes it, before any ordinal is assigned.

    Deliberately without an ordinal: numbering is the outline's to do, because
    only the outline knows which ordinals the book has already handed out and
    must never hand out again.
    """

    key: str = Field(pattern=BOOK_ID_PATTERN, description="Stable slug for the chapter")
    title: str = Field(description="The chapter's title")
    thesis: str = Field(default="", description="What the chapter argues, in one line")


class ProposedReference(BaseModel, frozen=True):
    """One cross-reference as a layout proposes it, by chapter key."""

    from_key: str = Field(description="Key of the chapter that refers")
    to_key: str = Field(description="Key of the chapter referred to")
    relation: ChapterRelation = Field(description="What the referring chapter does")
    subject: str = Field(description="What the referring chapter needs from the other")


class BookLayout(BaseModel):
    """A book as the book stage laid it out, before ordinals are assigned.

    Keys throughout rather than ordinals, so the stage that lays a book out
    never has to know — or guess — which numbers are already spoken for.
    """

    title: str = Field(default="", description="What the book is called")
    chapters: list[ProposedChapter] = Field(
        default=[], description="The chapters, in reading order"
    )
    references: list[ProposedReference] = Field(
        default=[], description="What each chapter needs from the others"
    )


class BookOutline(BaseModel):
    """A book's reading order, its assigned ordinals, and its cross-references.

    The one thing above the chapters: written by the book stage, read by every
    chapter run. ``chapters`` is the order to read in and ``retired`` is the set
    of ordinals that were assigned and are no longer read — kept so that an
    ordinal is never handed to a second chapter, which is what makes it an
    identity rather than a position.
    """

    book: str = Field(pattern=BOOK_ID_PATTERN, description="Which book this outlines")
    title: str = Field(default="", description="What the book is called")
    chapters: list[ChapterEntry] = Field(
        default=[], description="The book's chapters, in reading order"
    )
    retired: list[ChapterEntry] = Field(
        default=[],
        description=(
            "Chapters dropped from the reading order, holding their ordinals so "
            "nothing is ever renumbered onto one a reader already saw"
        ),
    )
    cross_references: list[CrossReference] = Field(
        default=[], description="What each chapter needs from the others"
    )

    def every(self) -> list[ChapterEntry]:
        """Every chapter this book has ever assigned an ordinal to."""
        return [*self.chapters, *self.retired]

    def entry(self, ordinal: int) -> ChapterEntry | None:
        """The chapter holding one ordinal, where the reading order still has it."""
        return next((held for held in self.chapters if held.ordinal == ordinal), None)

    def next_ordinal(self) -> int:
        """An ordinal this book has never assigned, retirements included."""
        return max((held.ordinal for held in self.every()), default=0) + 1

    def resolved(self) -> list[ResolvedReference]:
        """Every cross-reference looked up in the order that assigned it.

        A reference whose endpoints are no longer both in the reading order is
        reported and left out: it points at a chapter nobody reads, so a stage
        shown it would be asked to honor a link into a gap.
        """

        def looked_up() -> Iterator[ResolvedReference]:
            """Each reference paired with the two chapters it names."""
            for reference in self.cross_references:
                source = self.entry(reference.from_chapter)
                target = self.entry(reference.to_chapter)
                if source is None or target is None:
                    logger.warning(
                        "Cross-reference %d -> %d in %s names a chapter the "
                        "reading order no longer holds",
                        reference.from_chapter,
                        reference.to_chapter,
                        self.book,
                    )
                    continue
                yield ResolvedReference(
                    reference=reference, source=source, target=target
                )

        return list(looked_up())

    def references_from(self, ordinal: int) -> list[ResolvedReference]:
        """What one chapter is expected to lean on."""
        return [r for r in self.resolved() if r.reference.from_chapter == ordinal]

    def references_to(self, ordinal: int) -> list[ResolvedReference]:
        """What the other chapters expect from this one."""
        return [r for r in self.resolved() if r.reference.to_chapter == ordinal]

    def laid_out(self) -> "BookLayout":
        """This outline in the vocabulary a layout speaks: keys, not ordinals.

        What the book stage is shown of the book it is laying out again — the
        same shape it writes, so the keys it reads back are the keys it can
        spell. A key kept is an ordinal kept, since the key is what
        :meth:`relaid` holds a chapter's number against, and a stage shown only
        titles would have to invent one and renumber the chapter it named.
        """
        key_of = {entry.ordinal: entry.key for entry in self.chapters}
        return BookLayout(
            title=self.title,
            chapters=[
                ProposedChapter(key=held.key, title=held.title, thesis=held.thesis)
                for held in self.chapters
            ],
            references=[
                ProposedReference(
                    from_key=key_of[reference.from_chapter],
                    to_key=key_of[reference.to_chapter],
                    relation=reference.relation,
                    subject=reference.subject,
                )
                for reference in self.cross_references
                if reference.from_chapter in key_of and reference.to_chapter in key_of
            ],
        )

    def relaid(self, layout: "BookLayout", *, unused: int) -> "BookOutline":
        """This book laid out again as ``layout`` proposes, ordinals held fixed.

        A chapter already on record keeps the ordinal it has, whatever its new
        place in the reading order; a chapter this outline has never seen takes
        one the book has never used; and a chapter the layout dropped moves to
        ``retired`` still holding its own. So a run of the book stage can
        reorder, retitle, insert, and drop without re-addressing a single
        chapter a reader has already been given a number for.

        ``unused`` is the first ordinal the *book* has never handed out, which
        the caller states because an outline cannot know it: a chapter run that
        appended itself without a layout holds an ordinal on file and nowhere
        here, and numbering from this outline alone would hand it out twice.
        """
        held = {entry.key: entry.ordinal for entry in self.every()}

        def numbered() -> Iterator[ChapterEntry]:
            """Each proposed chapter, keeping its ordinal or taking a fresh one."""
            nonlocal unused
            for proposed in layout.chapters:
                if proposed.key in held:
                    ordinal = held[proposed.key]
                else:
                    ordinal = unused
                    unused += 1
                yield ChapterEntry(
                    key=proposed.key,
                    ordinal=ordinal,
                    title=proposed.title,
                    thesis=proposed.thesis,
                )

        chapters = list(numbered())
        ordinal_of = {entry.key: entry.ordinal for entry in chapters}

        def carried() -> Iterator[CrossReference]:
            """Each proposed reference as ordinals, where both ends are chapters."""
            for reference in layout.references:
                if reference.from_key not in ordinal_of:
                    logger.warning(
                        "Cross-reference from %r names no chapter of this layout",
                        reference.from_key,
                    )
                    continue
                if reference.to_key not in ordinal_of:
                    logger.warning(
                        "Cross-reference to %r names no chapter of this layout",
                        reference.to_key,
                    )
                    continue
                yield CrossReference(
                    from_chapter=ordinal_of[reference.from_key],
                    to_chapter=ordinal_of[reference.to_key],
                    relation=reference.relation,
                    subject=reference.subject,
                )

        return BookOutline(
            book=self.book,
            title=layout.title or self.title,
            chapters=chapters,
            retired=sorted(
                (entry for entry in self.every() if entry.key not in ordinal_of),
                key=lambda entry: entry.ordinal,
            ),
            cross_references=list(carried()),
        )

    def render(self, placement: ChapterPlacement | None = None) -> str:
        """The book's order and its cross-references, as a chapter run reads it.

        With a ``placement``, the cross-references are narrowed to that
        chapter's own — what it must lean on and what later chapters will expect
        of it — because those are the ones its writers have to honor.
        """

        def lines() -> Iterator[str]:
            """The order, what is retired from it, then the references."""
            named = f" — {self.title}" if self.title else ""
            yield (
                f"## The book's recorded order{named}\n\n"
                f"A chapter's ordinal is its identity and is never reassigned, "
                f"so read the sequence below rather than the numbers:\n"
                + "\n".join(
                    f"- {held.label()}" + (f": {held.thesis}" if held.thesis else "")
                    for held in self.chapters
                )
            )
            if self.retired:
                yield (
                    "Ordinals held back so nothing reuses them: "
                    + ", ".join(held.label() for held in self.retired)
                )
            yield from self.reference_lines(placement)

        return "\n\n".join(lines())

    def reference_lines(self, placement: ChapterPlacement | None) -> Iterator[str]:
        """The cross-reference sections, narrowed to one chapter where given."""
        if placement is None:
            resolved = self.resolved()
            if resolved:
                yield "## Cross-references between chapters\n\n" + "\n".join(
                    f"- {r.render()}" for r in resolved
                )
            return
        leans_on = self.references_from(placement.chapter)
        if leans_on:
            yield (
                "## What this chapter is expected to lean on\n\n"
                "Use these — the terms and results they name were settled "
                "elsewhere in this book, so do not re-argue or rename them:\n"
                + "\n".join(f"- {r.render()}" for r in leans_on)
            )
        leaned_on = self.references_to(placement.chapter)
        if leaned_on:
            yield (
                "## What later chapters expect from this one\n\n"
                "Establish these here, under these names, or the chapters "
                "that point at them will have nothing to find:\n"
                + "\n".join(f"- {r.render()}" for r in leaned_on)
            )


class ChapterRecord(BaseModel):
    """What one chapter run wrote down for the chapters beside it.

    Self-describing on purpose: the file carries its own placement, so a
    chapter record read on its own says which book it belongs to instead of
    depending on the directory it was found in.
    """

    placement: ChapterPlacement = Field(description="Which chapter this records")
    title: str = Field(default="", description="The chapter's own title")
    thesis: str = Field(default="", description="What the chapter argues, in one line")
    sections: list[str] = Field(
        default=[], description="The chapter's section titles, in order"
    )
    written_at: datetime = Field(
        default_factory=utc_now, description="When the record was written"
    )

    def render(self) -> str:
        """This chapter as a sibling chapter's stage reads it."""

        def lines() -> Iterator[str]:
            """The heading, what the chapter argues, then its outline."""
            yield f"## Chapter {self.placement.chapter} — {self.title or 'untitled'}"
            if self.thesis:
                yield self.thesis
            if self.sections:
                yield "\n".join(f"- {title}" for title in self.sections)

        return "\n\n".join(lines())


class BookRecord(BaseModel):
    """Everything one book holds, read as one: its outline and its chapters.

    A book nobody has written to yet reads as this with no outline and no
    chapters rather than as a missing file every caller has to test for, so the
    first chapter of a book runs against the same code path as the ninth. An
    absent outline is not an empty one: it says the book stage has never laid
    this book out, which is exactly what a run appending a chapter has to know
    it is assuming.
    """

    book: str = Field(pattern=BOOK_ID_PATTERN, description="Which book this is")
    outline: BookOutline | None = Field(
        default=None,
        description=(
            "The book's reading order, ordinals, and cross-references, absent "
            "until the book stage has laid the book out"
        ),
    )
    chapters: list[ChapterRecord] = Field(
        default=[], description="Chapter records on file, in chapter order"
    )

    def chapter(self, ordinal: int) -> ChapterRecord | None:
        """What one chapter of this book recorded, where it has recorded any."""
        return next(
            (held for held in self.chapters if held.placement.chapter == ordinal), None
        )

    def besides(self, placement: ChapterPlacement) -> "BookRecord":
        """This book without the chapter ``placement`` names — the siblings.

        A run reading its own previous record would be reading its own stale
        output back as though a sibling had written it. Matched on the whole
        placement rather than on the ordinal, so a placement in another book
        drops nothing here instead of dropping this book's chapter of the same
        number. The outline is kept whole: it is the book's order rather than
        this chapter's output, and a run that dropped its own row from it would
        be reading a book with a hole where it sits.
        """
        return BookRecord(
            book=self.book,
            outline=self.outline,
            chapters=[held for held in self.chapters if held.placement != placement],
        )

    def next_ordinal(self) -> int:
        """An ordinal this book has never used, in its outline or on file."""
        outlined = self.outline.next_ordinal() if self.outline is not None else 1
        written = max((held.placement.chapter for held in self.chapters), default=0) + 1
        return max(outlined, written)

    def relaid(self, layout: BookLayout) -> BookOutline:
        """This book laid out as ``layout`` proposes, against every ordinal used.

        The book stage's own write. It goes through the record rather than
        through the outline because the record is what knows the whole of what
        has been handed out — the outline's chapters, what it retired, and the
        chapters a lone run appended with no outline to append to.
        """
        outline = (
            self.outline if self.outline is not None else BookOutline(book=self.book)
        )
        return outline.relaid(layout, unused=self.next_ordinal())

    def ordinal_titled(self, title: str) -> int | None:
        """The ordinal this book records under ``title``, where exactly one does.

        Exactly one, because the point of a lookup is to find a decision
        somebody already made. Two chapters answering to one title is not a
        decision, and picking either would misplace a whole chapter — so the
        answer is that the record does not say, and the caller assumes openly
        instead.
        """
        wanted = comparable(title)
        if not wanted:
            return None
        outlined = self.outline.chapters if self.outline is not None else []
        found = sorted(
            dict.fromkeys(
                [held.ordinal for held in outlined if comparable(held.title) == wanted]
                + [
                    held.placement.chapter
                    for held in self.chapters
                    if comparable(held.title) == wanted
                ]
            )
        )
        return found[0] if len(found) == 1 else None

    def keyed(self, key: str) -> ChapterEntry | None:
        """The chapter ``key`` names in the reading order, where it still holds one.

        The reading order and not every ordinal ever assigned, because this is
        what a reference is resolved through: a retired chapter is a page the
        book no longer serves, so nothing may be addressed to it.
        """
        return entry_keyed(
            self.outline.chapters if self.outline is not None else [], key
        )

    def written(self, key: str) -> ChapterRecord | None:
        """What the chapter ``key`` names wrote down, where it has been written.

        The other half of :meth:`address`, and what keeps an unfinished book
        from reading as a broken one: chapter three points at chapter seven
        before anyone has written chapter seven, and that reference resolving
        to nothing is the plan working rather than a fault. Only a reference
        whose target is on file was ever expected to resolve, so this is what a
        reader of the two answers together asks second.

        Asked of every ordinal the book has ever assigned, retirements
        included, because it answers about the past where :meth:`address`
        answers about what is served now. A chapter dropped from the reading
        order was written all the same and keeps its record on file, and a
        reference into it is exactly the link that ships broken — reusing the
        reading-order lookup here would import that refusal, which is right
        for addressing and wrong for this, and the reference would go
        unreported precisely because it cannot resolve.

        A key no layout ever declared answers None whether or not some chapter
        of this book happens to carry that title: until the book stage names
        it, nothing has given the key an ordinal, which is exactly the state
        :func:`~inkwell.agent.book_links.pointing` tells a writer to expect.
        """
        entry = entry_keyed(
            self.outline.every() if self.outline is not None else [], key
        )
        return None if entry is None else self.chapter(entry.ordinal)

    def address(self, target: BookTarget) -> BookAddress | None:
        """Where ``target`` sits in this book, once something has numbered it.

        The lookup a reference in a chapter's prose is resolved through, and
        the whole of what resolution is: a key the layout declared, read back
        as the ordinal that layout assigned it. Nothing here assigns one — an
        ordinal is an identity and handing one out is the outline's to do — so
        a chapter this book has not laid out, a chapter dropped from the
        reading order, a section its chapter has not recorded, and a section
        its chapter gives no ordinal all answer None. The caller says so in the
        document rather than inventing a number or renumbering the target to
        suit the sentence that wanted it.

        Which ordinal a recorded section holds is :func:`placed_sections`'s to
        say and not restated here, because the reader-feedback export routes
        by the same answer off the same titles: a section named beside
        numbered ones holds no ordinal, so a reference to it goes unresolved
        rather than resolving onto whichever section really holds that number.

        The chapter is the outline's, not the one a section title happens to
        open with, on the same grounds the plan's placement outranks a carried
        over prefix: the outline assigned that ordinal and the prefix only
        remembers where the section once sat.
        """
        entry = self.keyed(target.chapter)
        if entry is None:
            return None
        if not target.section:
            return ChapterAddress(chapter=entry.ordinal)
        held = self.chapter(entry.ordinal)

        def named() -> Iterator[int]:
            """The ordinal of each recorded section this reference could name."""
            for placed in placed_sections(held.sections if held is not None else []):
                if placed.section is not None and placed.answers(target.section):
                    yield placed.section

        ordinal = next(named(), None)
        if ordinal is None:
            return None
        return SectionAddress(chapter=entry.ordinal, section=ordinal)

    def identify(self, assignment: ChapterAssignment, title: str) -> ChapterIdentity:
        """Which chapter of this book a run writing ``title`` is.

        The launch wins where it said: an explicit ordinal is a decision the
        author made, and a title match is at best a decision they made earlier
        and at worst a coincidence, so the lookup never overrules one. Where the
        launch left it open the book's own record answers, and where the record
        is silent the run appends — saying so, since an unstated append is
        exactly the invented order this is meant to avoid.
        """
        declared = assignment.declared()
        if declared is not None:
            return ChapterIdentity(placement=declared, source="declared")
        recorded = self.ordinal_titled(title)
        if recorded is not None:
            return ChapterIdentity(
                placement=ChapterPlacement(book=self.book, chapter=recorded),
                source="recorded",
            )
        return ChapterIdentity(
            placement=ChapterPlacement(book=self.book, chapter=self.next_ordinal()),
            source="appended",
        )

    def render(self, placement: ChapterPlacement | None = None) -> str:
        """The book so far, as a chapter run reads it.

        With a ``placement`` the cross-reference sections are that chapter's
        own; without one the whole book's are rendered, which is what a run that
        does not yet know which chapter it is has to read.
        """

        def lines() -> Iterator[str]:
            """The header, the recorded order, then each chapter on file."""
            yield (
                f"# {self.book} — the book so far\n\n"
                f"{len(self.chapters)} chapter(s) on record, written by earlier "
                f"runs. Read them as what this book has already claimed and "
                f"already named: keep their terms, and do not re-argue what "
                f"they settled."
            )
            if self.outline is None:
                yield (
                    "This book has no recorded order yet — no book stage has "
                    "laid it out, so nothing here says which chapter follows "
                    "which, or what any chapter owes another."
                )
            else:
                yield self.outline.render(placement)
            yield from (held.render() for held in self.chapters)

        return "\n\n".join(lines()) + "\n"


class BookStore(BaseModel, frozen=True):
    """The book records on disk, addressed by book identity.

    Holds no state beyond its root: every read resolves a path and every write
    lands one file, which keeps the layout — rather than this class — the thing
    a reader has to understand, exactly as the corpus store does.
    """

    root: Path

    def book_dir(self, book: str) -> Path:
        """Where one book's outline and chapter records sit."""
        return self.root / book

    def outline_path(self, book: str) -> Path:
        """The one file the book stage writes: this book's order."""
        return self.book_dir(book) / "outline.json"

    def chapter_path(self, placement: ChapterPlacement) -> Path:
        """The one file a chapter run writes, zero-padded so a listing sorts.

        Derived from the placement and nothing else, which is what makes the
        single-writer-per-chapter guarantee structural: a run holding its own
        placement cannot address a sibling chapter's file.
        """
        return self.book_dir(placement.book) / f"{placement.chapter:03d}.json"

    def load_outline(self, book: str) -> BookOutline | None:
        """This book's recorded order, or None where no book stage has laid one.

        An unreadable outline reads as none rather than taking the book down: a
        chapter run states what it assumed and carries on, and the next run of
        the book stage rewrites the file.
        """
        path = self.outline_path(book)
        if not path.is_file():
            return None
        try:
            return BookOutline.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            logger.warning("Unreadable book outline at %s", path)
            return None

    def load(self, book: str) -> BookRecord:
        """Everything one book holds, empty where nothing has been written yet.

        Constructing the empty record first is what checks the identity, so no
        unvalidated name reaches a path. A chapter file that no longer parses is
        reported and left out rather than taking the whole book down — the other
        chapters are still readable, and re-running that chapter rewrites it.
        """
        empty = BookRecord(book=book)
        directory = self.book_dir(empty.book)
        if not directory.is_dir():
            return empty
        outline = self.outline_path(empty.book)

        def held() -> Iterator[ChapterRecord]:
            """Each chapter file under this book that still parses as one."""
            for path in sorted(directory.glob("*.json")):
                if path == outline:
                    continue
                try:
                    yield ChapterRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (ValidationError, OSError):
                    logger.warning("Unreadable chapter record at %s", path)

        return BookRecord(
            book=empty.book,
            outline=self.load_outline(empty.book),
            chapters=sorted(held(), key=lambda record: record.placement.chapter),
        )

    def publish(self, record: ChapterRecord) -> Path:
        """Write one chapter's record, replacing whatever that chapter had.

        A run publishes its own chapter and no other, and the write is atomic,
        so a concurrent run of another chapter is unaffected and a concurrent
        reader of the book sees this record whole or not yet.
        """
        path = self.chapter_path(record.placement)
        publish_atomic(path, record)
        return path

    def publish_outline(self, outline: BookOutline) -> Path:
        """Write the book's order, replacing whatever order it had.

        The book stage's write and nobody else's: a chapter run reads this file
        and appends to the book by publishing its own chapter record, so the
        single-writer partition holds here the way it holds per chapter.
        """
        path = self.outline_path(outline.book)
        publish_atomic(path, outline)
        return path

    def books(self) -> tuple[str, ...]:
        """Every book the store holds a chapter record or an outline for."""
        if not self.root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and any(entry.glob("*.json"))
            )
        )
