"""How long one part of a work may run, given what the rest of it runs to.

A part run cannot answer this for itself. Asked how long a subsection on cyber
risk should be, a run holding that subsection and nothing else answers from the
subject — and the subject is inexhaustible, so the answer is "longer". A
thousand words in, eight thousand out, and the chapter now has one subsection
carrying more prose than the eight around it together. Nothing in the run was
wrong; the question was addressed to the wrong reader.

The work can answer it, and answer it without spending anything: every part's
text is on disk, so what a part holds, what its siblings hold, and what its
chapter runs to are three reads. That is the book-level view this needs, and
the whole of it.

**A budget is a delivery contract.** The plan may choose what fits inside it,
but a writer and rewriter cannot silently enlarge the book. If the evidence
truly needs more room, the work's allocation must change before another run.
"""

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from inkwell.manuscript.graph import source_text
from inkwell.manuscript.tree import GROWTH_ALLOWANCE, Manuscript, ManuscriptNode


class LengthBudget(BaseModel):
    """What one part holds, what its chapter holds, and the room in between.

    Declared rather than rendered straight into a sentence, so the numbers are
    available to something that wants to check a draft against them rather than
    only to a writer being told about them.
    """

    model_config = ConfigDict(frozen=True)

    holds: int = Field(default=0, description="Words this part holds now")
    chapter: str = Field(
        default="", description="What the chapter it sits in is called"
    )
    chapter_words: int = Field(
        default=0, description="Words that chapter holds across all its parts"
    )
    siblings: int = Field(
        default=0, description="How many parts the chapter is divided into"
    )
    allowance: float = Field(
        default=GROWTH_ALLOWANCE,
        description="How much longer than it stands the part may run unasked",
    )

    def ceiling(self) -> int:
        """The longest this part runs without somebody having asked for it."""
        return int(self.holds * self.allowance)

    def floor(self) -> int:
        """The shortest, for the same reason pointed the other way."""
        return int(self.holds / self.allowance)

    def share(self) -> int:
        """What an evenly divided chapter would give each of its parts."""
        return self.chapter_words // self.siblings if self.siblings else 0

    def render(self) -> str:
        """This budget as the sentence a writer is handed."""
        if not self.holds:
            return ""
        placed = (
            f" Its chapter, {self.chapter}, runs to {self.chapter_words:,} words "
            f"across {self.siblings} parts, averaging {self.share():,} each."
            if self.siblings > 1
            else ""
        )
        return (
            f"This part runs to {self.holds:,} words as the work holds it."
            f"{placed} Land between {self.floor():,} and "
            f"{self.ceiling():,} words. The final submission is accepted only "
            f"inside that interval. This rules out growing past it because the subject is "
            f"large, which every subject is — a part that arrives at several "
            f"times the length of its siblings has answered a question about "
            f"the book's shape that nothing in this run was in a position to ask."
        )


def chapter_key(key: str) -> str:
    """The chapter one part sits in, read off its key.

    A key is a path down the tree, so the chapter is its first step. Read
    through the path type rather than cut out of the string, for the same
    reason the store flattens keys that way: a key that gained a trailing
    slash still names the same chapter.
    """
    parts = PurePosixPath(key).parts
    return parts[0] if parts else ""


def budget_for(
    manuscript: Manuscript,
    node: ManuscriptNode,
    *,
    allowance: float | None = None,
) -> LengthBudget:
    """How long this part may run, given what its chapter already runs to.

    Reads the chapter rather than the work. The comparison a part's length is
    meaningful against is its siblings' — a subsection three times the length
    of every other subsection in its chapter is out of scale whatever the rest
    of the book does — and reading one chapter costs a handful of files where
    reading the work costs all of them, on every part of every pass.

    The allowance comes from the work unless a caller states one, so a book
    that means its parts to grow says so once at import instead of every
    reader of this rediscovering that the default did not suit it.
    """
    root = Path(manuscript.root)
    # lup: ignore[dict-str-payload] — a cache keyed by whatever paths a work has
    opened: dict[str, str | None] = {}
    chapter = manuscript.node(chapter_key(node.key))

    def words(held: ManuscriptNode) -> int:
        """How long one part runs, zero where its text is not there to read."""
        return len((source_text(root, held, opened) or "").split())

    def within() -> Iterator[ManuscriptNode]:
        """Every part of this part's chapter, itself included."""
        return iter(chapter.leaves()) if chapter is not None else iter((node,))

    siblings = tuple(within())
    return LengthBudget(
        holds=words(node),
        chapter=chapter.title if chapter is not None else "",
        chapter_words=sum(words(held) for held in siblings),
        siblings=len(siblings),
        allowance=(allowance if allowance is not None else manuscript.growth_allowance),
    )
