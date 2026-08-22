"""The shared vocabulary a work already declares, and which parts lean on it.

An imported work is not a blank page, and this is the clearest case of it. The
Atlas ships ``docs/includes/abbreviations.md`` — two hundred and thirty-nine
entries of ``*[Term]: meaning``, which mkdocs applies across every page. That
is a book-wide term ledger, written by the authors, and it is exactly the
thing the propagation graph most needs: what one part changes about a term,
every part using that term depends on.

**So it is imported, not invented.** The alternative — waiting for runs to
coin terms into a ledger of our own — would leave a work of two hundred parts
with an empty dependency graph until every part had been rewritten once, which
is precisely the pass the graph exists to avoid. Read the authors' file and
the graph is dense from the first pass.

**Usage is a fact about the text, not a guess about it.** mkdocs substitutes
an abbreviation wherever the term appears as a word, so a part *uses* a term
exactly when its prose contains it — this is the work's own semantics being
read back, not an inference over it. Matching is case-sensitive and
word-bounded for the same reason: the file declares ``Superintelligence``,
``superintelligent``, and ``super-intelligence`` as separate entries precisely
because mkdocs tells them apart, and folding them together here would report a
part as depending on a term the published book never substituted into it.

A run may still coin something the authors never declared, and that lands in
the store beside this rather than in the authors' file — see
:class:`~inkwell.agent.glossary.NodeGlossary` for the read order, in which the
authors' vocabulary comes first and therefore binds.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.glossary import ChapterGlossary, GlossaryEntry
from inkwell.manuscript.facts import Dependency

logger = logging.getLogger(__name__)

ENTRY_OPEN = "*["
"""How an abbreviation entry begins, in the syntax the extension defines."""

ENTRY_CLOSE = "]:"
"""What separates the term from its meaning on an entry's line."""

IMPORT_RUN = "import"
"""What the imported vocabulary records as the run that wrote it.

Named rather than left empty so a reader of the file can tell the authors'
own terms from anything a run coined, without consulting the read order.
"""

WORD_CHARACTERS = "_"
"""Characters that join a word besides the alphanumerics.

A term is matched at word boundaries, and what counts as inside a word is the
question that decides whether "AI" is found in "chain". A hyphen is
deliberately absent: "AI-driven" does use the term, and the published book
substitutes it there.
"""


def joins_a_word(character: str) -> bool:
    """Whether a character continues the word beside it."""
    return character.isalnum() or character in WORD_CHARACTERS


def mentions(text: str, term: str) -> bool:
    """Whether ``text`` uses ``term`` as a word rather than inside a longer one.

    Scanned by position rather than matched by pattern: what is being asked is
    where a literal string sits and what is either side of it, which is a
    lookup, and a pattern would have to escape the term to ask the same thing.
    """
    if not term:
        return False
    at = text.find(term)
    while at != -1:
        before = text[at - 1] if at else ""
        after = text[at + len(term) :][:1]
        if not joins_a_word(before) and not joins_a_word(after):
            return True
        at = text.find(term, at + 1)
    return False


class Abbreviation(BaseModel):
    """One entry of a work's declared vocabulary."""

    model_config = ConfigDict(frozen=True)

    term: str = Field(description="What the work calls it")
    meaning: str = Field(description="What the work says it means")


def parsed_entry(line: str) -> Abbreviation | None:
    """One line of the abbreviations file, or nothing where it declares none.

    Blank lines, the file's own comments, and anything else the authors keep
    there are not errors — the file is theirs, and this reads the entries it
    recognises rather than asserting what else may be in it.
    """
    if not line.startswith(ENTRY_OPEN):
        return None
    close = line.find(ENTRY_CLOSE)
    if close == -1:
        return None
    term = line[len(ENTRY_OPEN) : close].strip()
    meaning = line[close + len(ENTRY_CLOSE) :].strip()
    return Abbreviation(term=term, meaning=meaning) if term else None


def abbreviations_in(text: str) -> tuple[Abbreviation, ...]:
    """Every term one abbreviations file declares, in the order it declares them."""

    def declared() -> Iterator[Abbreviation]:
        """Each line that declares a term."""
        for line in text.splitlines():
            if (entry := parsed_entry(line)) is not None:
                yield entry

    return tuple(declared())


def read_abbreviations(path: Path) -> tuple[Abbreviation, ...]:
    """A work's declared vocabulary, empty where it declares none.

    A work with no such file is normal rather than wrong: it simply has no
    author-declared vocabulary, and its ledger is whatever its runs coin.
    """
    if not path.is_file():
        return ()
    return abbreviations_in(path.read_text(encoding="utf-8"))


def as_glossary(entries: tuple[Abbreviation, ...]) -> ChapterGlossary:
    """A work's declared vocabulary in the shape the shared ledger reads.

    Written into the store as one more glossary file rather than kept in a
    parallel structure, so a writer reaching for ``lookup_terms`` is handed
    the authors' vocabulary through the same call that hands it what the runs
    coined, and first-definition-wins makes the authors' spelling the one that
    binds without any rule saying so.

    Several terms share one meaning where the authors declared them that way —
    ``Superintelligence`` and ``superintelligent`` carry the same sentence.
    They stay separate entries: the published book substitutes each spelling
    where it appears, so a part using one is not thereby using the other, and
    folding them would report dependencies the work does not have.
    """
    return ChapterGlossary(
        run=IMPORT_RUN,
        terms=[
            GlossaryEntry(term=entry.term, meaning=entry.meaning) for entry in entries
        ],
    )


def terms_used(text: str, entries: tuple[Abbreviation, ...]) -> tuple[Dependency, ...]:
    """Every declared term one part's prose actually uses.

    The consumption record a part has before any run has touched it, which is
    what lets a first pass over a work propagate anything at all.
    """
    return tuple(
        Dependency(kind="term", subject=entry.term)
        for entry in entries
        if mentions(text, entry.term)
    )
