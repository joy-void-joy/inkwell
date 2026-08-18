"""Where a work's tree and its state live between runs.

A run of the pipeline dies with its session. A work of two hundred parts is
revised over months, so everything this system knows about one has to outlive
every run that touched it — which is the same argument ``BookStore`` makes for
cross-chapter records, and this sits beside it for the same reason.

**Never in the work's own repository.** The Atlas belongs to the people who
write it; a build directory dropped into their tree is this system helping
itself to somebody else's checkout. It also would not survive a fresh clone,
which is exactly when the state is most worth having.

**The tree is a cache; the state is the record.** Reading the work produces
the same tree every time for the same files, so ``tree.json`` is written for
what wants to read the structure without a source checkout to hand — a status
display, the web tree — and can be thrown away without losing anything. The
state cannot be recomputed from anything, and is the only file here that
matters.

**One writer, and it is the loop.** Node runs write prose, into their own
spans, and hand back what they consumed and what they changed; the loop that
scheduled them is the only thing that writes state. That is a stronger
guarantee than a lock and a cheaper one, and the ``running`` standing with its
holder is what makes it checkable — a part already held by a run is one the
loop will not schedule twice. Each write is a temp-and-rename, so anything
reading alongside the loop sees one whole state or the one before it.
"""

import logging
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ValidationError

from lup.channels.models import publish_atomic

from inkwell.manuscript.state import WorkState
from inkwell.manuscript.tree import Manuscript

logger = logging.getLogger(__name__)

WORK_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
"""What a work's identity may be spelled with: a lowercase hyphenated slug.

The same shape a book identity takes, and for the same two reasons: every run
against one work has to spell it identically months apart, and it names a
directory, so no title can walk out of the store's root. What the work is
*called* is prose and lives in its tree.
"""

TREE_FILE = "tree.json"
"""The imported structure, written for readers with no source checkout."""

STATE_FILE = "state.json"
"""Standings, build stamps, and the change ledger — the part that is a record."""

GLOSSARY_DIR = "glossary"
"""Where each part coins its terms, one file per part."""


def flattened(key: str) -> str:
    """One part's key as a single filename.

    A key is a path down the tree, and read as one rather than rewritten as a
    string: a directory per level would bury a flat listing several deep for
    nothing, and normalising through the path type means a key that gained a
    trailing slash somewhere still names one file.
    """
    return ".".join(PurePosixPath(key).parts)


class ManuscriptStore(BaseModel, frozen=True):
    """One work's records on disk, addressed by the work's identity.

    Holds nothing but its root: every read resolves a path and every write
    lands one file, so the layout rather than this class is what a reader has
    to understand — as the corpus store and the book store both do.
    """

    root: Path

    def work_dir(self, work: str) -> Path:
        """Where one work's records sit."""
        return self.root / work

    def tree_path(self, work: str) -> Path:
        """The file holding one work's imported structure."""
        return self.work_dir(work) / TREE_FILE

    def state_path(self, work: str) -> Path:
        """The file holding everything this system knows about one work."""
        return self.work_dir(work) / STATE_FILE

    def glossary_dir(self, work: str) -> Path:
        """Where one work's per-part glossary files sit.

        A directory of its own rather than files beside the state, so a
        listing of one kind cannot pick up the other.
        """
        return self.work_dir(work) / GLOSSARY_DIR

    def glossary_path(self, work: str, key: str) -> Path:
        """The one glossary file a part coins into.

        Derived from the part's key and nothing else, which is what makes the
        single-writer-per-part guarantee structural: a run holding its own key
        has no way to name a sibling's file.
        """
        return self.glossary_dir(work) / f"{flattened(key)}.json"

    def load_tree(self, work: str) -> Manuscript | None:
        """One work's recorded structure, or nothing where none was written.

        An unreadable tree reads as none rather than raising: it is a cache of
        an import, so the repair is to import again, and a caller with the
        source to hand should do exactly that instead of being stopped.
        """
        path = self.tree_path(work)
        if not path.is_file():
            return None
        try:
            return Manuscript.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            logger.warning("Unreadable work tree at %s — import it again", path)
            return None

    def load_state(self, work: str) -> WorkState:
        """Everything this system knows about one work, empty where it is new.

        Unlike the tree, an unreadable state is raised rather than swallowed.
        There is nothing to fall back to: carrying on with an empty state would
        present every part as never built and rewrite a whole work, which is
        the most expensive possible reading of a corrupt file.
        """
        path = self.state_path(work)
        if not path.is_file():
            return WorkState()
        return WorkState.model_validate_json(path.read_text(encoding="utf-8"))

    def publish_tree(self, work: str, manuscript: Manuscript) -> Path:
        """Write one work's structure, atomically."""
        path = self.tree_path(work)
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_atomic(path, manuscript)
        return path

    def publish_state(self, work: str, state: WorkState) -> Path:
        """Write one work's state, atomically.

        The loop calls this after every part it finishes, rather than once at
        the end of a pass: a pass over two hundred parts that lost its record
        on an interrupt would rebuild all of them.
        """
        path = self.state_path(work)
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_atomic(path, state)
        return path

    def works(self) -> tuple[str, ...]:
        """Every work this store holds, in name order."""
        if not self.root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if (entry / STATE_FILE).is_file() or (entry / TREE_FILE).is_file()
            )
        )
