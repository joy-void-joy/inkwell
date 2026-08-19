"""The shared term ledger: what a piece calls things, and who agrees on it.

Section writers run in parallel and a book's chapters run months apart, so what
something is *called* has to live outside any one writer and outside any one
run. That is this ledger: a writer coins into it, every other writer reads it,
and the first definition of a term is the one that binds.

Three scopes, one shape. A standalone article's ledger is one file in the run's
own notes and dies with them, because no later run needs the terms it settled.
A book chapter's ledger is a file per chapter under the book's record, read in
chapter order — see :mod:`inkwell.agent.book` for the partition and why it
needs no lock. An imported work's ledger is that partition taken down to the
part, and reads the vocabulary its authors declared before anything a run
coined. A row measures a draft against the same reading a writer gets,
through :func:`read_glossary` and :meth:`BookGlossary.canon`, so what the
writer is handed and what the draft is checked against cannot disagree.

The engine lives here and its tool surface lives in
:mod:`inkwell.agent.tools.stage_outputs`, so a caller that only needs to read
what the piece has named — a format check, a merge — reaches the reader without
reaching a tool server.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from lup.channels.models import publish_atomic

from inkwell.agent.book import BookStore, ChapterPlacement
from inkwell.manuscript.store import ManuscriptStore

DECLARED_VOCABULARY = "declared"
"""The key the authors' own vocabulary is filed under.

Not a part's key — no part of a work is called this, so nothing a run coins
can land on the file, and the read order can name it without a lookup.
"""


class GlossaryEntry(BaseModel):
    """One shared term: a name, symbol, or abbreviation and the single meaning
    every section must use for it."""

    term: str = Field(description="The term, name, symbol, or abbreviation")
    meaning: str = Field(description="Its canonical meaning/usage for this piece")
    aliases: list[str] = Field(
        default=[],
        description=(
            "The rival names for this same thing that the piece does not use — "
            "what a writer weighed and set aside. Recording one is what makes a "
            "later rename knowable: a chapter reaching for a rejected name is "
            "handed this term instead, and a draft that slips into one is "
            "reported"
        ),
    )

    def answers_to(self, name: str) -> bool:
        """Whether `name` refers to this entry — its term, or a name it rejected."""
        return name.casefold() in {
            self.term.casefold(),
            *(alias.casefold() for alias in self.aliases),
        }

    def rejected(self) -> list[str]:
        """The names this entry turned down, which its own term is not.

        A writer listing the term among its own aliases turned nothing down,
        and a row matching on that would report a passage using the canonical
        name as a rename of itself.
        """
        return [
            alias for alias in self.aliases if alias.casefold() != self.term.casefold()
        ]


class DefineTermResult(BaseModel):
    """Result of define_term — the canonical entry the caller should conform to."""

    term: str
    meaning: str
    already_defined: bool = Field(
        description=(
            "True when a sibling writer already defined this term; the returned "
            "meaning is the canonical one — conform to it rather than your own."
        )
    )


class GlossaryView(BaseModel):
    """The glossary as a writer reads it: conventions, and what has been coined."""

    conventions: list[str] = Field(
        default=[],
        description="Plan-level conventions this chapter seeded before writing",
    )
    terms: list[GlossaryEntry] = Field(
        default=[],
        description=(
            "Terms coined so far, in book order — the other chapters of this "
            "book first, then what sibling writers here have coined"
        ),
    )


class ChapterGlossary(BaseModel, extra="ignore"):
    """One glossary file: what a chapter seeded, what it coined, and whose run.

    Distinct from :class:`GlossaryView` because the file has to record which
    run wrote it, and a writer reading the glossary has no use for that.
    """

    run: str = Field(
        default="",
        description="Which run last seeded this chapter — see seed_glossary",
    )
    conventions: list[str] = Field(
        default=[], description="What this chapter's plan seeded"
    )
    terms: list[GlossaryEntry] = Field(
        default=[], description="What this chapter's writers coined"
    )


class RunGlossary(BaseModel, frozen=True):
    """The glossary of a piece that belongs to no book: one file, this run's.

    It lives in the run's own notes and dies with them, which is right for a
    standalone article: there is no later run whose writers would need the
    terms this one settled.
    """

    path: Path

    def own(self) -> Path:
        """The one file this run coins into."""
        return self.path

    def read_order(self) -> tuple[Path, ...]:
        """Every file a term may already be defined in, in that order."""
        return (self.path,)

    def canon(self) -> GlossaryView | None:
        """What a book has already named — nothing, this piece being in no book.

        A rival name for a term a sibling section coined this afternoon is
        drift the merge resolves while both writers are still running. The
        drift no single run can see is the cross-chapter one, so that is what a
        row measures, and here there are no other chapters to measure against.
        """
        return None


class BookGlossary(BaseModel, frozen=True):
    """A book's glossary, partitioned one file per chapter.

    A chapter coins into its own file and reads every chapter's, so a term one
    chapter settles binds the next chapter to run — months later, in another
    process — while no two chapter runs of one book ever write the same path.

    Reading in chapter order is what keeps first-definition-wins deterministic
    when two chapters coined one term without seeing each other: the lower
    ordinal is the one a reader of the book meets first, so it is the one that
    wins, for every writer and for the merge that follows.
    """

    store: BookStore
    placement: ChapterPlacement

    def own(self) -> Path:
        """The one file this chapter coins into, and the only one it writes."""
        return self.store.glossary_path(self.placement)

    def read_order(self) -> tuple[Path, ...]:
        """Every chapter's file, in chapter order."""
        directory = self.store.glossary_dir(self.placement.book)
        return tuple(sorted(directory.glob("*.json")))

    def canon(self) -> GlossaryView | None:
        """Every name this book has settled, whichever chapter settled it."""
        return read_glossary(self)


class NodeGlossary(BaseModel, frozen=True):
    """A work's glossary, partitioned one file per part.

    The book partition taken down to the unit anybody revises, and with one
    addition the book has no equivalent of: an imported work arrives with a
    vocabulary its authors already declared, and that file is read *first*.
    First-definition-wins then makes the authors' spelling the one that binds,
    without a rule anywhere saying the authors outrank a run — the read order
    is the rule.

    Sorting the rest by filename is what keeps the order deterministic between
    two parts that coined the same term without seeing each other. It is not
    reading order, and deliberately not: reading order changes when a work is
    rearranged, and a term would then change meaning because a chapter moved.
    """

    store: ManuscriptStore
    work: str
    key: str

    def own(self) -> Path:
        """The one file this part coins into, and the only one it writes."""
        return self.store.glossary_path(self.work, self.key)

    def read_order(self) -> tuple[Path, ...]:
        """The authors' declared vocabulary, then every part's own file."""
        declared = self.store.glossary_path(self.work, DECLARED_VOCABULARY)
        directory = self.store.glossary_dir(self.work)
        coined = sorted(path for path in directory.glob("*.json") if path != declared)
        return (declared, *coined)

    def canon(self) -> GlossaryView | None:
        """Every name this work has settled, whoever settled it."""
        return read_glossary(self)


GlossaryScope = RunGlossary | BookGlossary | NodeGlossary
"""Where one run's glossary lives — its own notes, its book's record, or the
work whose part it is revising."""


def load_chapter_glossary(path: Path) -> ChapterGlossary:
    """Read one chapter's glossary file, empty when nothing has been written."""
    if not path.exists():
        return ChapterGlossary()
    return ChapterGlossary.model_validate_json(path.read_text(encoding="utf-8"))


def write_chapter_glossary(path: Path, glossary: ChapterGlossary) -> None:
    """Persist one chapter's glossary file, atomically.

    A run of a neighbouring chapter may read this file at any moment, and the
    temp-and-rename is what has it see either the whole glossary or the one
    before it, never a half-written mix of the two.
    """
    publish_atomic(path, glossary)


def read_glossary(scope: GlossaryScope) -> GlossaryView:
    """The conventions this chapter seeded and every term the piece has coined.

    Conventions come from this chapter alone — a plan's conventions are its
    instructions to its own writers, so re-running chapter one is not governed
    by a plan written for chapter nine — while terms come from the whole book,
    because what something is called is a fact about the book rather than an
    instruction from any one plan.
    """
    return GlossaryView(
        conventions=load_chapter_glossary(scope.own()).conventions,
        terms=[
            entry
            for path in scope.read_order()
            for entry in load_chapter_glossary(path).terms
        ],
    )


def seed_glossary(scope: GlossaryScope, run: str, conventions: list[str]) -> None:
    """Seed this chapter's glossary with its plan's conventions.

    What the chapter coined is kept when the same run seeds again — a resumed
    write stage must not lose the terms its finished sections already used —
    and dropped when a different run does, so a term the chapter has since
    withdrawn stops binding every later chapter rather than outliving the prose
    that introduced it. Either way this writes the chapter's own file, so no
    other chapter's coinages are in reach to discard.
    """
    held = load_chapter_glossary(scope.own())
    write_chapter_glossary(
        scope.own(),
        ChapterGlossary(
            run=run,
            conventions=conventions,
            terms=held.terms if held.run == run else [],
        ),
    )


def define_term_in_glossary(
    scope: GlossaryScope, term: str, meaning: str, aliases: list[str] | None = None
) -> DefineTermResult:
    """Register a term unless the book already has one — first definition wins.

    The whole book is scanned, so a chapter about to rename what an earlier one
    defined is handed the earlier meaning instead; only this chapter's own file
    is written, so a chapter run of the same book coining at the same moment is
    never writing here. A name an earlier entry recorded as one it rejected
    resolves to that entry, so first-definition-wins reaches a rename and not
    only a respelling.
    """
    for entry in read_glossary(scope).terms:
        if entry.answers_to(term):
            return DefineTermResult(
                term=entry.term, meaning=entry.meaning, already_defined=True
            )
    held = load_chapter_glossary(scope.own())
    coined = GlossaryEntry(term=term, meaning=meaning, aliases=aliases or [])
    write_chapter_glossary(
        scope.own(), held.model_copy(update={"terms": [*held.terms, coined]})
    )
    return DefineTermResult(term=term, meaning=meaning, already_defined=False)
