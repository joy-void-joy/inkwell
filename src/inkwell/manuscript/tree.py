"""The tree a work of many parts is, and what state each part is in.

Structure and state are separate models on purpose. The tree is what the
source says — read again on every import, and the same every time for the same
files. State is what this system knows about the work: which parts somebody
asked to revise, which are mid-run, which are waiting on an answer. Held in
one model they would be rewritten together, and re-importing a work would
throw away everything anybody had asked of it.

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


type NodeStatus = Literal["clean", "dirty", "running", "parked", "failed"]
"""Where one part stands.

'dirty' is the one that drives everything: it means somebody asked for this
part to change, or something it depends on already did. A pass is finished
when nothing is dirty, which is what gives a loop that never ends a resting
state that it can be at.
"""


class NodeState(BaseModel):
    """What this system knows about one part, as against what the source says."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The node this is about")
    status: NodeStatus = Field(default="clean", description="Where the part stands")
    reason: str = Field(
        default="",
        description="Why it is in that state, in the words of whoever put it "
        "there — an author's instruction, or the node whose change reached it",
    )
    changed_at: datetime = Field(
        default_factory=utc_now, description="When the status last moved"
    )


class WorkState(BaseModel):
    """Every part's state, kept beside the tree rather than inside it.

    A part the source has but nobody has touched has no entry, and reads as
    clean. Recording only what moved is what keeps a re-import from having an
    opinion about state it was never told anything about.
    """

    states: list[NodeState] = Field(
        default_factory=list, description="One entry per part that has moved"
    )

    def status(self, key: str) -> NodeStatus:
        """Where a part stands, clean where nothing has moved it."""
        found = next((entry for entry in self.states if entry.key == key), None)
        return found.status if found else "clean"

    def marked(self, key: str, status: NodeStatus, reason: str) -> "WorkState":
        """This state with one part moved, replacing any entry it already had."""
        kept = [entry for entry in self.states if entry.key != key]
        return WorkState(
            states=[*kept, NodeState(key=key, status=status, reason=reason)]
        )

    def pending(self) -> list[NodeState]:
        """Every part with work outstanding, which is what a pass has left to do."""
        return [entry for entry in self.states if entry.status != "clean"]
