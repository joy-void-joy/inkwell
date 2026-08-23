"""The work as somebody reads it: one document per chapter, one tab per part.

A part run makes its own Google Doc, and until now made a new one every time it
ran — four runs of one subsection made four documents. A pass over two hundred
parts makes two hundred, and the next pass makes two hundred more, none of which
anybody opens twice. What an author actually wants to read is a chapter, and
what they want to comment on is a passage of it.

So the document chain runs the other way: subsection markdown projects up into a
chapter document, and every chapter projects up into one book document. Tabs
nest three deep in Docs, which is exactly chapter/section/subsection, so the
structure of the work *is* the structure of the document and nothing has to be
laid out twice.

**A tab is a part, which is why there is no routing table.** A comment lands in
a tab, the tab holds one part's prose, and the passage it quotes is a passage of
that part — so which part an author is talking about is a lookup rather than a
guess. That is the whole mechanism by which feedback reaches the run that can
act on it.

**Read and comment, never edit.** What comes back from a document is comments;
what does not come back is prose. Edits belong in the markdown, where the
sweep's ``source-moved`` verdict already notices them and the next pass builds
against them. A document that were also an editing surface would be a second
copy of the book that could disagree with the first, and every projection would
have to decide which one won.
"""

import logging
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.tools.google_docs import (
    CommentEntry,
    do_create_doc,
    do_create_tab,
    do_fetch_comments,
    do_write_tab,
)
from inkwell.corpus.storage import now_stamp
from inkwell.manuscript.graph import source_text
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

logger = logging.getLogger(__name__)


def parent_key(key: str) -> str:
    """The node one level above this one, empty at the top of the tree.

    Read through the path type rather than cut out of the string, because a
    key is a path down the tree and that is what the type is for.
    """
    held = PurePosixPath(key).parent.as_posix()
    return "" if held == "." else held


class PartTab(BaseModel):
    """One part of a work and the tab holding it."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part, by the key state uses")
    tab_id: str = Field(description="The tab its prose is written into")


class ChapterDoc(BaseModel):
    """One chapter's document, and which tab each of its parts is in."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The chapter, by the key state uses")
    doc_id: str = Field(description="The document it projects into")
    url: str = Field(default="", description="Where an author reads it")
    tabs: tuple[PartTab, ...] = Field(
        default=(), description="One entry per node that has a tab"
    )

    def tab_for(self, key: str) -> str:
        """The tab one node is in, empty where it has none yet."""
        return next((one.tab_id for one in self.tabs if one.key == key), "")

    def with_tab(self, key: str, tab_id: str) -> "ChapterDoc":
        """This chapter with one node's tab recorded, replacing any it had."""
        kept = tuple(one for one in self.tabs if one.key != key)
        return self.model_copy(
            update={"tabs": (*kept, PartTab(key=key, tab_id=tab_id))}
        )


class WorkDocs(BaseModel):
    """Every document a work projects into, and what a run may reuse.

    A record rather than a cache: a document id cannot be recomputed, and
    losing one does not lose the document — it leaves an orphan in somebody's
    Drive and makes a second one beside it, which is the failure this exists to
    stop happening two hundred times a pass.
    """

    chapters: tuple[ChapterDoc, ...] = Field(
        default=(), description="One projected document per chapter"
    )
    book_doc_id: str = Field(
        default="", description="The document every chapter projects into"
    )
    book_url: str = Field(
        default="", description="Where an author reads the whole work"
    )
    part_docs: tuple[PartTab, ...] = Field(
        default=(),
        description="The working document each part's runs reuse, by part key. A "
        "run that made a fresh one every time made four documents for four runs "
        "of one subsection",
    )
    seen_comments: tuple[str, ...] = Field(
        default=(),
        description="Comment ids already taken in. Read twice without this, one "
        "comment asks for its part again on every sweep and the loop never rests",
    )
    projected_at: str = Field(default="", description="When the projection last ran")

    def chapter(self, key: str) -> ChapterDoc | None:
        """The document one chapter projects into, where it has one."""
        return next((one for one in self.chapters if one.key == key), None)

    def with_chapter(self, held: ChapterDoc) -> "WorkDocs":
        """This record with one chapter's document replaced whole."""
        kept = tuple(one for one in self.chapters if one.key != held.key)
        return self.model_copy(update={"chapters": (*kept, held)})

    def working_doc(self, key: str) -> str:
        """The document one part's runs write into, empty where it has none."""
        return next((one.tab_id for one in self.part_docs if one.key == key), "")

    def with_working_doc(self, key: str, doc_id: str) -> "WorkDocs":
        """This record with one part's working document noted."""
        kept = tuple(one for one in self.part_docs if one.key != key)
        return self.model_copy(
            update={"part_docs": (*kept, PartTab(key=key, tab_id=doc_id))}
        )

    def taken_in(self, ids: Iterable[str]) -> "WorkDocs":
        """This record with these comments marked as already acted on."""
        return self.model_copy(
            update={"seen_comments": tuple(dict.fromkeys((*self.seen_comments, *ids)))}
        )


def chapters_of(manuscript: Manuscript) -> tuple[ManuscriptNode, ...]:
    """Every top-level part of the work, which is what gets a document."""
    return tuple(manuscript.children)


def held_text_of(manuscript: Manuscript, node: ManuscriptNode) -> str:
    """What one node holds, its descendants' prose included, in reading order.

    A node with no children carries its own text; one with children carries its
    leaves', in order. What makes this one function rather than two is a
    section file the import read as a single part: it has no children and still
    reads whole.
    """
    root = Path(manuscript.root)
    # lup: ignore[dict-str-payload] — a cache keyed by whatever paths a work has
    opened: dict[str, str | None] = {}

    def prose() -> Iterator[str]:
        """This node's own text, or each of its leaves'."""
        held = (node,) if not node.children else tuple(node.leaves())
        for one in held:
            yield source_text(root, one, opened) or ""

    return "\n\n".join(one for one in prose() if one.strip())


async def project_chapter(
    manuscript: Manuscript, chapter: ManuscriptNode, held: ChapterDoc | None
) -> ChapterDoc:
    """Write one chapter into its document, creating what is not there yet.

    Idempotent in both directions: a chapter that has a document keeps it, and
    a tab that exists is written over rather than added beside. That is what
    makes this safe to run after every part lands, which is when an author
    wants to see it.
    """
    doc = held
    if doc is None:
        created = await do_create_doc(f"{manuscript.title} — {chapter.title}")
        doc = ChapterDoc(key=chapter.key, doc_id=created.doc_id, url=created.url)
        logger.info("Projected %s into a new document at %s", chapter.key, created.url)

    for node in chapter.walk():
        if node.key == chapter.key or not node.path:
            continue
        parent = doc.tab_for(parent_key(node.key))
        tab_id = doc.tab_for(node.key) or await do_create_tab(
            doc.doc_id, node.title, parent_tab_id=parent or None
        )
        doc = doc.with_tab(node.key, tab_id)
        await do_write_tab(doc.doc_id, tab_id, held_text_of(manuscript, node))
    return doc


async def project_book(manuscript: Manuscript, docs: WorkDocs) -> WorkDocs:
    """Write the whole work into one document, a tab per chapter.

    Synced once per pass rather than per part: it is the reading surface for
    somebody who wants the book rather than a chapter of it, and rebuilding a
    two-hundred-part document every time one part lands would spend most of the
    projection on the surface fewest people are looking at.
    """
    held = docs
    if not held.book_doc_id:
        created = await do_create_doc(manuscript.title)
        held = held.model_copy(
            update={"book_doc_id": created.doc_id, "book_url": created.url}
        )
        logger.info(
            "Projected %s into a new document at %s", manuscript.title, created.url
        )

    for chapter in chapters_of(manuscript):
        tab_id = await do_create_tab(held.book_doc_id, chapter.title)
        await do_write_tab(held.book_doc_id, tab_id, held_text_of(manuscript, chapter))
    return held.model_copy(update={"projected_at": now_stamp()})


async def project(
    manuscript: Manuscript, docs: WorkDocs, *, book: bool = True
) -> WorkDocs:
    """Project every chapter, and the book above them."""
    held = docs
    for chapter in chapters_of(manuscript):
        held = held.with_chapter(
            await project_chapter(manuscript, chapter, held.chapter(chapter.key))
        )
    return await project_book(manuscript, held) if book else held


def carrying(manuscript: Manuscript, quoted: str) -> str:
    """The part whose prose holds this passage, empty where none does.

    What makes a tab a part rather than a place: a comment quotes what it is
    about, the quote is a span of one part's text, and finding which part is a
    lookup over prose this system wrote. Read off the text rather than off the
    comment's anchor, because an anchor is Drive's own encoding of a position
    in a document that is rewritten every pass, and a position that moves is
    not an address.

    Whitespace is collapsed on both sides before comparing: a document's line
    breaks are its own, and a quote that came back wrapped differently from how
    the markdown wrapped it is the same passage.
    """
    root = Path(manuscript.root)
    # lup: ignore[dict-str-payload] — a cache keyed by whatever paths a work has
    opened: dict[str, str | None] = {}
    wanted = " ".join(quoted.split())
    if not wanted:
        return ""
    for node in manuscript.leaves():
        text = " ".join((source_text(root, node, opened) or "").split())
        if wanted in text:
            return node.key
    return ""


class Feedback(BaseModel):
    """One author comment, and the part it turned out to be about."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part it is about")
    comment_id: str = Field(description="What Drive addresses it by")
    author: str = Field(default="", description="Who left it")
    said: str = Field(description="The comment, and any replies, as one note")

    def render(self) -> str:
        """This comment as the reason a part is asked for."""
        named = f"{self.author}: " if self.author else ""
        return f"{named}{self.said}"


def noted(entry: CommentEntry, key: str) -> Feedback:
    """One comment as the note its part is given, replies included.

    Replies included because a thread is one piece of feedback: a comment
    saying "this is wrong" and a reply saying "actually it is the figure that
    is wrong" are the same instruction, and handing over only the first sends a
    run to fix the wrong thing.
    """
    return Feedback(
        key=key,
        comment_id=entry.comment_id,
        author=entry.author,
        said=" / ".join((entry.content, *entry.replies)),
    )


def routed(
    manuscript: Manuscript, comments: Iterable[CommentEntry], seen: Iterable[str] = ()
) -> tuple[Feedback, ...]:
    """Each new comment, addressed to the part whose prose it quotes.

    A comment already taken in is skipped rather than re-read: acted on twice,
    one comment asks for its part again on every sweep and the loop never
    settles. An unanchored comment, or one quoting text no part holds any more,
    belongs to no part and is reported apart — routing it to whichever part
    happened to match would be worse than saying nothing.
    """
    already = tuple(seen)

    def found() -> Iterator[Feedback]:
        """Each comment paired with the part carrying what it quotes."""
        for entry in comments:
            if entry.comment_id in already or entry.resolved:
                continue
            key = carrying(manuscript, entry.anchor_text)
            if key:
                yield noted(entry, key)

    return tuple(found())


def unrouted(
    manuscript: Manuscript, comments: Iterable[CommentEntry], seen: Iterable[str] = ()
) -> tuple[CommentEntry, ...]:
    """Each new comment no part claims, which is a person's to place.

    Reported rather than dropped. A comment on a passage an author has since
    rewritten quotes text the work no longer holds, and silently ignoring it
    would lose exactly the feedback somebody took the trouble to leave.
    """
    already = tuple(seen)
    return tuple(
        entry
        for entry in comments
        if entry.comment_id not in already
        and not entry.resolved
        and not carrying(manuscript, entry.anchor_text)
    )


async def comments_on(docs: WorkDocs) -> tuple[CommentEntry, ...]:
    """Every comment on every document this work projects into."""

    def documents() -> Iterator[str]:
        """Each chapter's document, then the book's."""
        for chapter in docs.chapters:
            yield chapter.doc_id
        if docs.book_doc_id:
            yield docs.book_doc_id

    async def gathered() -> Iterator[CommentEntry]:
        """Every comment on every one of them, in document order."""
        found = [await do_fetch_comments(one) for one in documents()]
        return (entry for page in found for entry in page)

    return tuple(await gathered())
