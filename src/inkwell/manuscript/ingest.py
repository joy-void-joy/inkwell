"""Read a work off disk into the tree the pipeline runs against.

An mkdocs work already declares everything this needs and declares it better
than a filesystem walk could guess: ``.pages.yml`` gives each part's title and
its reading order, so ``03.md`` is known to be "2.3 - Misuse Risks" and known
to come fifth. Ordering is the thing a walk gets wrong — ``A1.md`` sorts
before ``01.md``, and the appendix belongs at the end.

Below the file, the headings are the structure. A section file holds one H1
naming the section and an H2 per subsection, and the subsection is the unit
anybody revises — "2.3.2 Cyber Risk" is a heading inside ``02/03.md``, not a
file of its own. So the tree goes one level past the filesystem, and a part
that shares a file with its siblings records the heading it starts at rather
than a path nobody could open.

The ``.pages.yml`` and ``.meta.yml`` mappings are other tools' files, read
through the open-dict hatch on purpose: what mkdocs and a chapter's own
metadata put in them is theirs to decide, and a model here would be this
project asserting a schema over somebody else's format.
"""

import logging
from itertools import takewhile
from pathlib import Path

import yaml
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from lup.types import JsonObject, JsonValue

from inkwell.corpus.discovery import slugified
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

logger = logging.getLogger(__name__)

MARKDOWN = MarkdownIt()
"""The parser the headings are read with, rather than matched out of lines."""

PAGES_FILE = ".pages.yml"
"""What mkdocs-awesome-pages calls the file declaring a directory's order."""

META_FILE = ".meta.yml"
"""What the Atlas calls a chapter's own metadata — title, authors, links."""

SUBSECTION_LEVEL = "h2"
"""The heading a subsection opens with.

One below the H1 that names the section file itself, which is the shape every
Atlas section file has: ``# 2.3 Misuse Risks`` then ``## 2.3.1 Bio Risk``.
"""

ATTRIBUTE_OPEN = "{:"
"""Where an mkdocs attribute list starts, if a heading carries one.

``## 2.3.2 Cyber Risk {: #02}`` names the anchor, which is presentation rather
than title and would otherwise reach the tree as part of what the part is
called.
"""

ORDINAL_SEPARATOR = "."
"""What divides one level of a heading's numbering from the next."""

COLLISION_MARK = "~"
"""What distinguishes a key a second heading wanted and could not have.

Deliberately not a character a heading's own numbering can produce, so a
distinguished key can never be mistaken for one an author wrote.
"""


class PagesEntry(BaseModel):
    """One line of a nav declaration: a titled part, or a titled group of them."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(description="How the nav names it")
    target: str = Field(default="", description="The file it points at, if one")
    group: list["PagesEntry"] = Field(
        default_factory=list, description="Nested entries, for a nav group"
    )


def read_yaml(path: Path) -> JsonObject:
    """One YAML mapping off disk, empty where there is nothing to read."""
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def declared(mapping: JsonObject, key: str) -> JsonValue:
    """One key of another tool's file, or nothing where it says nothing."""
    # lup: ignore[dict-get] — mkdocs' own file, whose keys are mkdocs' to choose
    return mapping.get(key)


def nav_entries(raw: JsonValue) -> list[PagesEntry]:
    """A nav declaration as entries, however the file happened to spell it.

    Three shapes appear in one Atlas file: a bare filename, a ``Title: file``
    mapping, and a ``Group:`` mapping whose value is a nested list. The bare
    ``'...'`` wildcard means "everything else in order" and is skipped, since
    what it stands for is read off the directory instead.
    """

    def entry(item: JsonValue) -> PagesEntry | None:
        """One nav item, or nothing where it declares no part."""
        if isinstance(item, str):
            return None if item == "..." else PagesEntry(title=item, target=item)
        if not isinstance(item, dict):
            return None
        for title, value in item.items():
            if isinstance(value, list):
                return PagesEntry(title=str(title), group=nav_entries(value))
            return PagesEntry(title=str(title), target=str(value))
        return None

    if not isinstance(raw, list):
        return []
    return [found for item in raw if (found := entry(item)) is not None]


def ordinal_of(title: str) -> str:
    """The numbering a heading opens with, empty where it opens with none.

    "2.3.2 Cyber Risk" gives ``2.3.2``, which is what a reader calls it and
    what a cross-reference from another chapter would name it by. A trailing
    dot is dropped, so "3. Something" and "3 Something" key alike.

    A level of the numbering is not always digits. The Atlas numbers its
    appendices "1.A1.1 Surveys", "1.A1.2 Quotes", and so on, so a run of
    digits alone stops at the first "A" and hands every subsection of an
    appendix the same key — which loses all but one of them, since the key is
    the identity everything else is filed under. What the numbering is made of
    is therefore whatever a heading puts between the dots, and it is a
    numbering at all only because it opens with a digit.
    """

    def numbering(character: str) -> bool:
        """Whether a character is still part of the leading numbering."""
        return character.isalnum() or character == ORDINAL_SEPARATOR

    leading = "".join(takewhile(numbering, title.strip()))
    ordinal = leading.removesuffix(ORDINAL_SEPARATOR)
    return ordinal if ordinal[:1].isdigit() else ""


def heading_slug(title: str) -> str:
    """A part's key within its parent: its numbering, else its words."""
    return ordinal_of(title) or slugified(title.lower()) or "part"


def titled(heading: str) -> str:
    """A heading's title, with any mkdocs attribute list taken off the end."""
    marker = heading.find(ATTRIBUTE_OPEN)
    return (heading[:marker] if marker != -1 else heading).strip()


def heading_titles(text: str) -> list[str]:
    """Every subsection heading in a section file, in order.

    Read off the parsed tokens rather than matched out of lines, so a ``##``
    inside a fenced code block is not mistaken for a part of the work.
    """
    tokens = MARKDOWN.parse(text)
    return [
        titled(following.content)
        for token, following in zip(tokens, tokens[1:])
        if token.type == "heading_open" and token.tag == SUBSECTION_LEVEL
    ]


def subsections(text: str, parent_key: str, path: str) -> list[ManuscriptNode]:
    """Every H2 in a section file, as the parts they are.

    Keyed on the heading's own numbering rather than on position, so inserting
    a subsection does not renumber the ones after it and lose their state. A
    file with no H2 at all is one undivided part and yields nothing — the file
    itself is then the leaf.

    Two headings can still want one key: a file that numbers nothing and
    repeats a title, or one an author numbered twice by hand. The key is the
    identity every stamp, standing, and glossary file is addressed by, so a
    collision does not produce a confusing tree — it produces a work with a
    part missing, and no sign that it ever had one. So the second and later
    claimants take a distinguished key and the collision is reported: an ugly
    key is recoverable and a vanished subsection is not.
    """
    # lup: ignore[dict-str-payload] — a tally keyed by whatever slugs a file yields
    taken = dict[str, int]()

    def keyed(title: str) -> str:
        """A key for this heading that no earlier heading here has taken."""
        slug = heading_slug(title)
        seen = taken[slug] if slug in taken else 0
        taken[slug] = seen + 1
        if not seen:
            return f"{parent_key}/{slug}"
        logger.warning(
            "%s: %r wants the key %r that an earlier heading took — filing it "
            "under %r so it is not lost",
            path,
            title,
            f"{parent_key}/{slug}",
            f"{parent_key}/{slug}{COLLISION_MARK}{seen}",
        )
        return f"{parent_key}/{slug}{COLLISION_MARK}{seen}"

    return [
        ManuscriptNode(
            key=keyed(title),
            kind="subsection",
            title=title,
            path=path,
            heading=title,
        )
        for title in heading_titles(text)
    ]


def section_node(
    entry: PagesEntry, parent_key: str, base: Path, root: Path
) -> ManuscriptNode | None:
    """One section file as a node, with its subsections under it."""
    target = base / entry.target
    if not target.is_file():
        return None
    key = f"{parent_key}/{Path(entry.target).stem}"
    relative = target.relative_to(root).as_posix()
    text = target.read_text(encoding="utf-8")
    return ManuscriptNode(
        key=key,
        kind="section",
        title=entry.title,
        path=relative,
        children=subsections(text, key, relative),
    )


def chapter_node(directory: Path, root: Path) -> ManuscriptNode:
    """One chapter directory as a node, ordered by its own nav declaration."""
    pages = read_yaml(directory / PAGES_FILE)
    meta = read_yaml(directory / META_FILE)
    key = directory.name
    named = declared(meta, "chapter_title") or declared(pages, "title")
    title = str(named) if named is not None else key

    def parts(entries: list[PagesEntry], parent: str) -> list[ManuscriptNode]:
        """Each nav entry as a node, groups kept as the groups they are."""

        def built(entry: PagesEntry) -> ManuscriptNode | None:
            """One entry as its node, or nothing where its file is missing."""
            if not entry.group:
                return section_node(entry, parent, directory, root)
            group_key = f"{parent}/{heading_slug(entry.title)}"
            return ManuscriptNode(
                key=group_key,
                kind="group",
                title=entry.title,
                children=parts(entry.group, group_key),
            )

        return [node for entry in entries if (node := built(entry)) is not None]

    return ManuscriptNode(
        key=key,
        kind="chapter",
        title=title,
        children=parts(nav_entries(declared(pages, "nav")), key),
    )


def read_manuscript(chapters_dir: Path, *, title: str = "") -> Manuscript:
    """Read a whole work from the directory holding its chapters.

    Chapter directories are taken in name order, which is what the Atlas's own
    zero-padded ``01``..``09`` already encodes — its top-level nav declares
    only ``index.md`` and a wildcard, so there is no per-chapter order to read.
    """
    root = chapters_dir.resolve()
    directories = sorted(
        entry for entry in root.iterdir() if entry.is_dir() and entry.name[:1].isdigit()
    )
    return Manuscript(
        title=title or root.parent.name,
        root=str(root),
        children=[chapter_node(directory, root) for directory in directories],
    )
