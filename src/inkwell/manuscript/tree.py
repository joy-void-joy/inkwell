"""The tree a work of many parts is.

Structure only, and separate from state on purpose. The tree is what the
source says — read again on every import, and the same every time for the same
files. What this system *knows* about a work, which parts somebody asked to
revise and what each was last built from, lives in :mod:`.state`. Held in one
model they would be rewritten together, and re-importing a work would throw
away everything anybody had asked of it.

Identity is the path down the tree, so a node's key says where it sits without
a lookup, and two works can never collide on one. That is what lets state be
keyed on it and survive a re-import: a subsection keeps its key as long as the
file that holds it keeps its heading, which is exactly as long as it is
recognisably the same subsection.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lup.channels.models import utc_now

DEFAULT_WORK_FORMAT = "textbook"
"""What a work's parts are written as, unless whoever imported it says otherwise.

A work read as chapters holding sections holding subsections *is* a textbook,
and the format declaration of that name is what carries dependency order,
definitions-before-use, and self-containment into a part's run. The alternative
default is ``auto``, which asks each part's own extract stage to guess the
format of a book it is only shown one subsection of — and a guess that lands
differently on two parts of one work is worse than either answer.

Overridable rather than fixed, because a work of many parts need not be a
textbook: a collection of essays is imported the same way and wants its own
format.
"""

type NodeKind = Literal["work", "chapter", "section", "subsection", "group"]
"""What one node is, named rather than derived from its depth.

Depth alone would make the appendix group in the Atlas a chapter, since it
sits where chapters sit. A group holds parts without being one — it has no
text of its own and nothing runs against it.
"""


class ManuscriptNode(BaseModel):
    """One part of a work: what it is called, where its text lives, what is under it."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(
        description="Path down the tree, slash-separated — '02/03/2.3.2'. Stable "
        "across imports, which is what lets state key on it"
    )
    kind: NodeKind = Field(description="What this node is")
    title: str = Field(description="How the work names this part")
    path: str = Field(
        default="",
        description="Repository-relative file holding this part's text. Empty "
        "for a node that only contains others",
    )
    heading: str = Field(
        default="",
        description="For a part sharing a file with its siblings, the heading "
        "line it begins at, verbatim. Empty where the file is the part",
    )
    children: list["ManuscriptNode"] = Field(
        default_factory=list, description="The parts under this one, in reading order"
    )

    def walk(self) -> Iterator["ManuscriptNode"]:
        """This node and every node beneath it, in reading order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def leaves(self) -> Iterator["ManuscriptNode"]:
        """Every node with no parts under it — what a run actually rewrites."""
        for node in self.walk():
            if not node.children:
                yield node


class Manuscript(BaseModel):
    """A whole work, as imported from its source."""

    title: str = Field(description="What the work is called")
    root: str = Field(description="Directory the work was read from")
    target_format: str = Field(
        default=DEFAULT_WORK_FORMAT,
        description="What every part of this work is written as. Recorded on the "
        "work rather than asked per run, so pass three cannot answer it "
        "differently from pass one",
    )
    imported_at: datetime = Field(
        default_factory=utc_now, description="When the tree was last read"
    )
    children: list[ManuscriptNode] = Field(
        default_factory=list, description="The work's top-level parts, in order"
    )

    def walk(self) -> Iterator[ManuscriptNode]:
        """Every node in the work, in reading order."""
        for child in self.children:
            yield from child.walk()

    def leaves(self) -> Iterator[ManuscriptNode]:
        """Every part a run can be about."""
        for child in self.children:
            yield from child.leaves()

    def node(self, key: str) -> ManuscriptNode | None:
        """The node under ``key``, or nothing where the work has no such part."""
        return next((node for node in self.walk() if node.key == key), None)
