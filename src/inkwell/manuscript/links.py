"""The parts one part points at, read out of its own prose.

The structural half of the dependency graph. Where a work cross-links, a link
followed is exactly the dependency a build system would call an include, and
reading it is a parse rather than a guess — the same markdown-it tokens the
import already walks for headings.

**The Atlas has none of these.** Every non-http link target in its chapters is
an image, and it carries sixteen textual "chapter N" mentions across two
hundred parts. That is worth stating plainly rather than discovering twice: a
propagation graph keyed on declared cross-references alone would have been
empty for the work this was built for, which is why the shared vocabulary
carries it and this supplements. Other works link heavily, and for those this
is the cheaper and more exact edge.

**A link resolves to a part, or it is not a dependency.** An anchor into
another section names a subsection; a bare file names whatever part owns that
file. A target nothing in the tree answers to is a link out of the work — to
an image, to the web, to a page the tree does not model — and contributes no
edge rather than a broken one.
"""

import logging
from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import urlparse

from markdown_it.token import Token

from inkwell.manuscript.facts import Dependency
from inkwell.manuscript.ingest import MARKDOWN
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

logger = logging.getLogger(__name__)

MARKDOWN_SUFFIX = ".md"
"""What a link to another part of an mkdocs work ends in, where it names a file."""


def link_targets(text: str) -> tuple[str, ...]:
    """Every link target in one part's prose, in the order they appear.

    Read off the parsed tokens, so a URL inside a fenced block is code and a
    reference-style link resolves to what it refers to before being read.
    """

    def targets(tokens: list[Token]) -> Iterator[str]:
        """Each link's href, descending into the inline tokens that hold them."""
        for token in tokens:
            if token.type == "link_open":
                href = token.attrGet("href")
                if isinstance(href, str):
                    yield href
            if token.children:
                yield from targets(token.children)

    return tuple(targets(MARKDOWN.parse(text)))


def internal(target: str) -> bool:
    """Whether a target could name a part of this work rather than the web.

    A scheme or a host means it leaves the work, whatever it points at.
    """
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def resolved(
    work: Manuscript, holder: ManuscriptNode, target: str
) -> ManuscriptNode | None:
    """The part a link names, or nothing where it names none of them.

    Resolution is against the file a part lives in, since that is what a
    relative link in its prose is relative to. An anchor is deliberately not
    read as a subsection: an mkdocs anchor is a slug of a heading and a part's
    key is its numbering, so matching them would be a guess where the file is
    a lookup — the section that owns the file is the honest answer, and a
    section going out of date reaches its subsections through the tree.
    """
    if not internal(target) or not holder.path:
        return None
    path = urlparse(target).path
    if not path or not path.endswith(MARKDOWN_SUFFIX):
        return None
    named = (PurePosixPath(holder.path).parent / path).as_posix()
    wanted = PurePosixPath(named)
    return next(
        (
            node
            for node in work.walk()
            if node.path and PurePosixPath(node.path) == wanted
        ),
        None,
    )


def links_from(
    work: Manuscript, holder: ManuscriptNode, text: str
) -> tuple[Dependency, ...]:
    """Every part this one points at, as the dependencies they are.

    A part linking to itself contributes nothing: a run is never made stale by
    its own change, and an edge that could only ever say so is noise in a
    graph read by people.
    """

    def found() -> Iterator[Dependency]:
        """Each internal link that resolves to some other part of the work."""
        for target in link_targets(text):
            node = resolved(work, holder, target)
            if node is not None and node.key != holder.key:
                yield Dependency(kind="node", subject=node.key)

    return tuple(dict.fromkeys(found()))
