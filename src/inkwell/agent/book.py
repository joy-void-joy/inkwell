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

The record lives outside any session, beside the research corpus and for the
same reason — a session's notes die with the run, and a book assembled one
chapter per run would have nothing left to read.

**Concurrency.** One file per chapter, written by the run that owns that
chapter. Two chapter runs of one book therefore never write the same path, and
that partition — not a lock — is the guarantee: :meth:`BookStore.chapter_path`
derives the file from the record's own placement, so a run has no way to name
another chapter's file. Each write is an atomic temp-and-rename through
``publish_atomic``, so a reader assembling the book sees whole chapter records
or none, never a half-written one. Two runs of the *same* chapter do land on
one path; there the rename decides, and the later record replaces the earlier
one whole rather than blending with it.

    books/
    └── ai-safety-textbook/
        ├── 001.json     # one file per chapter, written by that chapter's run
        └── 003.json
"""

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

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
    """Every chapter record one book holds, read as one.

    A book nobody has written to yet reads as this with no chapters rather than
    as a missing file every caller has to test for, so the first chapter of a
    book runs against the same code path as the ninth.
    """

    book: str = Field(pattern=BOOK_ID_PATTERN, description="Which book this is")
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
        number.
        """
        return BookRecord(
            book=self.book,
            chapters=[held for held in self.chapters if held.placement != placement],
        )

    def render(self) -> str:
        """The book so far, as a chapter run reads it."""
        header = (
            f"# {self.book} — the book so far\n\n"
            f"{len(self.chapters)} chapter(s) on record, written by earlier runs. "
            f"Read them as what this book has already claimed and already named: "
            f"keep their terms, and do not re-argue what they settled.\n"
        )
        body = "\n\n".join(held.render() for held in self.chapters)
        return f"{header}\n{body}\n"


class BookStore(BaseModel, frozen=True):
    """The book records on disk, addressed by book identity.

    Holds no state beyond its root: every read resolves a path and every write
    lands one file, which keeps the layout — rather than this class — the thing
    a reader has to understand, exactly as the corpus store does.
    """

    root: Path

    def book_dir(self, book: str) -> Path:
        """Where one book's chapter records sit."""
        return self.root / book

    def chapter_path(self, placement: ChapterPlacement) -> Path:
        """The one file a chapter run writes, zero-padded so a listing sorts.

        Derived from the placement and nothing else, which is what makes the
        single-writer-per-chapter guarantee structural: a run holding its own
        placement cannot address a sibling chapter's file.
        """
        return self.book_dir(placement.book) / f"{placement.chapter:03d}.json"

    def load(self, book: str) -> BookRecord:
        """Every chapter of one book, empty where nothing has been written yet.

        Constructing the empty record first is what checks the identity, so no
        unvalidated name reaches a path. A chapter file that no longer parses is
        reported and left out rather than taking the whole book down — the other
        chapters are still readable, and re-running that chapter rewrites it.
        """
        empty = BookRecord(book=book)
        directory = self.book_dir(empty.book)
        if not directory.is_dir():
            return empty

        def held() -> Iterator[ChapterRecord]:
            """Each chapter file under this book that still parses as one."""
            for path in sorted(directory.glob("*.json")):
                try:
                    yield ChapterRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (ValidationError, OSError):
                    logger.warning("Unreadable chapter record at %s", path)

        return BookRecord(
            book=empty.book,
            chapters=sorted(held(), key=lambda record: record.placement.chapter),
        )

    def publish(self, record: ChapterRecord) -> Path:
        """Write one chapter's record, replacing whatever that chapter had.

        The only writer. A run publishes its own chapter and no other, and the
        write is atomic, so a concurrent run of another chapter is unaffected
        and a concurrent reader of the book sees this record whole or not yet.
        """
        path = self.chapter_path(record.placement)
        publish_atomic(path, record)
        return path

    def books(self) -> tuple[str, ...]:
        """Every book the store holds a chapter record for."""
        if not self.root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and any(entry.glob("*.json"))
            )
        )
