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

**One writer at a time.** A loop holds the work's exclusive lease for its
whole lifetime; bounded mutations such as requesting or clearing a part hold
the same lease for one write. Node runs write prose into disjoint spans and
hand back what they consumed and changed, leaving the loop as the only state
writer while a pass is active. Each write is a temp-and-rename, so anything
reading alongside it sees one whole state or the one before it.
"""

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, StringConstraints, ValidationError

from lup.channels.models import publish_atomic

from inkwell.manuscript.state import WorkState
from inkwell.manuscript.tree import Manuscript

if TYPE_CHECKING:
    # Imported for the annotations only: `findings` reaches the corpus and the
    # agent client, `chapters` reaches Google's, and the store is what a status
    # display loads to read a tree. Paying for either import to answer "which
    # works are there" would make the cheapest question in the system one of
    # the most expensive.
    from inkwell.manuscript.attending import AttendanceLog
    from inkwell.manuscript.chapters import WorkDocs
    from inkwell.manuscript.findings import WorkFindings
    from inkwell.manuscript.planner import WorkBriefs

logger = logging.getLogger(__name__)

WORK_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
"""What a work's identity may be spelled with: a lowercase hyphenated slug.

The same shape a book identity takes, and for the same two reasons: every run
against one work has to spell it identically months apart, and it names a
directory, so no title can walk out of the store's root. What the work is
*called* is prose and lives in its tree.
"""

type WorkId = Annotated[str, StringConstraints(pattern=WORK_ID_PATTERN)]
"""A work's identity, refused at the boundary rather than checked at each caller.

The pattern above says what an id may be; this is what makes saying so bind. A
surface that takes an id from outside declares it with this type and gets the
refusal for free, which is the difference between a documented constraint and an
enforced one.
"""

TREE_FILE = "tree.json"
"""The imported structure, written for readers with no source checkout."""

STATE_FILE = "state.json"
"""Standings, build stamps, and the change ledger — the part that is a record."""

GLOSSARY_DIR = "glossary"
"""Where each part coins its terms, one file per part."""

BRIEFS_FILE = "briefs.json"
"""What a book planner decided each outstanding part should become.

Stored because planning and running are different steps: a pass plans from
above every outstanding part and then runs them, concurrently and over hours.
Held only in the planner's memory, a brief would have to be handed down through
the loop — which would make the loop a thing that knows about briefs.
"""

DOCS_FILE = "docs.json"
"""Which documents this work projects into, and which each part's runs reuse.

The one file here that is neither a cache nor derivable. A document id cannot
be recomputed from anything, and losing one does not lose the document — it
leaves an orphan in somebody's Drive and makes a second one beside it, which is
the failure this exists to stop happening two hundred times a pass.
"""

FINDINGS_FILE = "findings.json"
"""What the research has been placed on, part by part.

Derived rather than recorded — the corpus holds the findings and the tree holds
the structure, so this could be computed again from both — and stored anyway,
because what dirties a part is the *difference* between this assignment and the
last one, and a difference needs the last one.
"""


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
        """Where one work's records sit, or a refusal if that is not under the root.

        Checked here rather than trusted from the caller, because this is the
        one place the id becomes a path and so the only place the danger is
        real. :data:`WorkId` refuses a malformed id at every surface that takes
        one from outside; this refuses the traversal itself, so a caller that
        never went through such a surface cannot reach out of the store either.
        """
        held = self.root / work
        if self.root.resolve() not in held.resolve().parents:
            raise ValueError(f"{work!r} does not name a work under {self.root}")
        return held

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

    def findings_path(self, work: str) -> Path:
        """The file holding what the research has been placed on."""
        return self.work_dir(work) / FINDINGS_FILE

    def load_findings(self, work: str) -> "WorkFindings":
        """What the research was last placed on, empty where nothing has been.

        An unreadable file reads as empty rather than raising, unlike the
        state: this is derived, so the worst an empty one costs is one sync
        reporting every finding as new, which is what a first sync does anyway.
        """
        from inkwell.manuscript.findings import WorkFindings

        path = self.findings_path(work)
        if not path.is_file():
            return WorkFindings()
        try:
            return WorkFindings.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            logger.warning("Unreadable findings at %s — sync them again", path)
            return WorkFindings()

    def publish_findings(self, work: str, found: "WorkFindings") -> Path:
        """Write what the research has been placed on, atomically."""
        path = self.findings_path(work)
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_atomic(path, found)
        return path

    def attending(self, work: str) -> "AttendanceLog":
        """Where the agents this work spends outside any part are recorded.

        A part run's agents live under that run's own artifacts and die with
        it, which is right — they are that run's. These outlive every run, so
        they are recorded against the work and any process that can read the
        work can read them.
        """
        from inkwell.manuscript.attending import attending

        return attending(self.work_dir(work))

    def briefs_path(self, work: str) -> Path:
        """The file holding what a book planner decided each part should become."""
        return self.work_dir(work) / BRIEFS_FILE

    def load_briefs(self, work: str) -> "WorkBriefs":
        """What was last planned, empty where nothing was.

        An unreadable one reads as empty rather than raising: a brief is a
        better plan, not a required one, and a part with none derives its own.
        """
        from inkwell.manuscript.planner import WorkBriefs

        path = self.briefs_path(work)
        if not path.is_file():
            return WorkBriefs()
        try:
            return WorkBriefs.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            logger.warning(
                "Unreadable briefs at %s — parts will require new briefs", path
            )
            return WorkBriefs()

    def publish_briefs(self, work: str, briefs: "WorkBriefs") -> Path:
        """Write what a book planner decided, atomically."""
        path = self.briefs_path(work)
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_atomic(path, briefs)
        return path

    def docs_path(self, work: str) -> Path:
        """The file holding which documents this work projects into."""
        return self.work_dir(work) / DOCS_FILE

    def load_docs(self, work: str) -> "WorkDocs":
        """Which documents this work has, empty where it has none.

        Unlike the tree, and like the state, this cannot be recomputed — but an
        unreadable one is reported and replaced rather than raised, because the
        cost of carrying on is a second set of documents and the cost of
        stopping is a pass that will not run.
        """
        from inkwell.manuscript.chapters import WorkDocs

        path = self.docs_path(work)
        if not path.is_file():
            return WorkDocs()
        try:
            return WorkDocs.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            logger.warning(
                "Unreadable document record at %s — a projection will make new "
                "documents rather than reuse what is there",
                path,
            )
            return WorkDocs()

    def publish_docs(self, work: str, docs: "WorkDocs") -> Path:
        """Write which documents this work projects into, atomically."""
        path = self.docs_path(work)
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_atomic(path, docs)
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
