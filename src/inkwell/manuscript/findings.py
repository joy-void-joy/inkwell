"""Which parts of a work the research has something new to say about.

The corpus reads documents; this decides where what they establish lands. It
is the second arrow of the build graph — ``document ──distil──▶ finding
──assign──▶ node assignment`` — and it is a separate arrow from the first for a
reason that is a fact about the world: documents do not change, and book
structure does. Keyed together, moving a subsection would re-read every paper
it touches. Kept apart, a restructure re-runs an assignment that reads titles
and costs nothing, over findings that were paid for once.

**It reads the tree's titles, never the book's prose.** What a subsection
titled "2.3.2 Cyber Risk" is about is answerable from its title and its place
in the work, and answering it that way is what makes the assignment affordable
to re-run and what keeps a single part from triggering a book read. A pass over
the whole book is the same call — the tree is small — so both paths through
this are one path.

**The sweep gates; this assigns.** What comes out of here is change facts, not
a verdict. A reader asked "does this part need work?" twice answers differently
twice, and a loop whose dirtiness signal is a judgement never settles. So a
finding that has landed on a part's node path becomes a fact in the ledger, the
sweep intersects it with what the part consumed exactly as it does every other
fact, and the loop picks the part up with a reason naming the paper. A sync
that assigns nothing new dirties nothing, which is settling by construction
rather than by care.
"""

import logging
from collections.abc import Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.client import query
from inkwell.agent.config import stage_model
from inkwell.corpus.distillation import Distillation, Finding
from inkwell.corpus.storage import now_stamp
from inkwell.manuscript.facts import ProposedChange, bears_on
from inkwell.manuscript.tree import Manuscript

logger = logging.getLogger(__name__)

CORPUS_ORIGIN = "corpus"
"""What a change fact from the research is recorded as having come from.

Not a part's key, and deliberately unlike one — the ledger's ``origin`` is
skipped when a part reads its own facts, so an origin that could collide with a
key would silently stop a part hearing about its own subject.
"""

ASSIGN_STAGE = "plan"
"""Which stage's model tier the assignment runs at.

A planner, and billed as one: it is reading a list of findings against a book's
table of contents and deciding what belongs where, which is the shape of the
question the plan stage answers. Cheap per call, because both sides are short.
"""

ASSIGN_SYSTEM = """\
You are placing research findings into a book that already exists.

You are given the book's structure — every part, by the key it is addressed by \
and the title its authors gave it — and a list of findings, each one something \
a document establishes. Say which parts each finding bears on.

A finding bears on a part when a reader of that part would be worse off not \
knowing it: it dates a claim the part is likely to make, it is the strongest \
recent evidence on the part's subject, it is a case the part's subject would \
now be incomplete without. Judge from the part's title and its place in the \
book, which is what you have — do not guess at prose you cannot see.

Most findings bear on one part, some on two, many on none. **None is the \
common and correct answer** for a finding about a subject this book does not \
cover, and it is the answer that keeps this useful: a finding placed on a part \
it merely touches will dirty that part, cost a full rewrite, and change \
nothing. Place a finding only where you would defend spending a rewrite on it.

Name parts by their key, exactly as given. A key you invent places the finding \
nowhere.
"""


class Sourced(BaseModel):
    """One finding together with the document it was read out of.

    Paired here rather than carried on the finding, because a finding is a
    property of the document's bytes and is cached under them: a copy holding
    its own provenance would be a second place the citation could be wrong.
    """

    model_config = ConfigDict(frozen=True)

    document: Distillation = Field(description="What it was read from")
    finding: Finding = Field(description="What that document establishes")


class Bearing(BaseModel):
    """One finding, placed on one part of a work.

    Flat rather than a finding holding a list of parts, because this is what
    gets compared between syncs: "is this finding on this part already" is a
    membership test on a flat record and a traversal of a nested one, and it is
    asked once per finding per pass.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The part it bears on, by the key state uses")
    document: str = Field(description="Content digest of the document it came from")
    claim: str = Field(description="What the document establishes")
    caveat: str = Field(default="", description="What its own authors attach to it")
    quote: str = Field(default="", description="A verbatim sentence carrying it")
    locator: str = Field(default="", description="Where in the document it is")
    cited: str = Field(default="", description="How a writer would name the document")
    why: str = Field(
        default="", description="Why it bears on this part rather than another"
    )

    def identity(self) -> str:
        """What makes this bearing the same one across two syncs.

        The document and the claim, not the wording of why it was placed: a
        reassignment that reaches the same conclusion in different words has
        moved nothing, and treating it as new would dirty the part on every
        pass and stop the loop ever settling.
        """
        return f"{self.document}::{self.claim}"

    def render(self) -> str:
        """This bearing as the sentence a part is told why it is out of date."""
        caveated = f" (caveat: {self.caveat})" if self.caveat else ""
        return f"{self.claim}{caveated} — {self.cited}"

    def change(self) -> ProposedChange:
        """This bearing as the fact that reaches the part it landed on."""
        return ProposedChange(dependency=bears_on(self.key), detail=self.render())


class WorkFindings(BaseModel):
    """Every finding the research has placed on a part of one work.

    Kept whole and replaced whole. It is a derived artifact — the assignment of
    findings the corpus already holds onto a tree the work already declares —
    so nothing here is a record that could not be recomputed, and what makes it
    worth storing is the *difference* between two of them.
    """

    bearings: tuple[Bearing, ...] = Field(
        default=(), description="Every finding placed, in the order it was placed"
    )
    vocabulary: str = Field(
        default="",
        description="Signature of the tag vocabulary the filter ran under. A "
        "retag changes which documents a filter reaches, so an assignment made "
        "under another vocabulary is answering a different question",
    )
    assigned_at: str = Field(default="", description="When the placing last ran")

    def on(self, key: str) -> tuple[Bearing, ...]:
        """Everything placed on one part."""
        return tuple(held for held in self.bearings if held.key == key)

    def identities(self) -> tuple[str, ...]:
        """What this assignment holds, as the comparison between syncs sees it."""
        return tuple(held.identity() for held in self.bearings)

    def keys(self) -> tuple[str, ...]:
        """Every part something has been placed on, each once."""
        return tuple(dict.fromkeys(held.key for held in self.bearings))


class Placement(BaseModel):
    """Where one finding goes, as the assigning pass answers."""

    model_config = ConfigDict(frozen=True)

    finding: int = Field(
        description="Which finding this is about, by its number in the list given"
    )
    keys: list[str] = Field(
        default=[],
        description="Keys of the parts it bears on, spelled exactly as given. "
        "Empty is the common and correct answer",
    )
    why: str = Field(
        default="", description="Why it bears on those parts rather than others"
    )


class Placements(BaseModel):
    """Where a whole list of findings goes."""

    placements: list[Placement] = Field(
        default=[], description="One entry per finding that bears on anything"
    )


def outlined(manuscript: Manuscript) -> str:
    """The work's structure as the assigning pass reads it.

    Titles and keys, indented by depth. This is the whole of what the pass is
    shown of the book, which is the point: reading the prose would cost the
    book on every sync and answer a question the titles already answer.
    """

    def lines() -> Iterator[str]:
        """Each node, placed by how deep in the work it sits."""
        for node in manuscript.walk():
            runnable = " ·" if not node.children else ""
            yield f"{'  ' * node.key.count('/')}- {node.key} — {node.title}{runnable}"

    return (
        f"# {manuscript.title}\n\nEvery part, by key. A '·' marks a part a run "
        f"can be about; the rest only contain others and nothing can be placed "
        f"on them.\n\n" + "\n".join(lines())
    )


def listed(held: Iterable[Sourced]) -> str:
    """The findings as a numbered list the pass places by number."""

    def lines() -> Iterator[str]:
        """Each finding, numbered, with the document it came from."""
        for number, one in enumerate(held, start=1):
            caveated = (
                f"\n   caveat: {one.finding.caveat}" if one.finding.caveat else ""
            )
            yield (
                f"{number}. {one.finding.claim}{caveated}\n"
                f"   from: {one.document.cited()}"
            )

    return "\n".join(lines())


def sourced(distillations: Iterable[Distillation]) -> tuple[Sourced, ...]:
    """Every finding these documents hold, each paired with where it came from."""
    return tuple(
        Sourced(document=document, finding=finding)
        for document in distillations
        for finding in document.findings
    )


def runnable_keys(manuscript: Manuscript) -> tuple[str, ...]:
    """Every part a finding can be placed on, which is every leaf with text."""
    return tuple(node.key for node in manuscript.leaves() if node.path)


def borne(
    manuscript: Manuscript,
    held: tuple[Sourced, ...],
    placements: Iterable[Placement],
) -> Iterator[Bearing]:
    """Each placement as the bearings it records, dropping what it invented.

    A key the pass made up places the finding nowhere, and silently: it would
    be a bearing on a part that does not exist, whose change fact nothing
    consumes. Dropped and logged instead, because a pass inventing keys is
    worth knowing about and a ledger full of facts addressed to nobody is not.
    """
    keys = runnable_keys(manuscript)
    for placement in placements:
        if not 1 <= placement.finding <= len(held):
            logger.warning(
                "A placement names finding %d, which was not asked about",
                placement.finding,
            )
            continue
        one = held[placement.finding - 1]
        for key in placement.keys:
            if key not in keys:
                logger.warning("A placement names %r, which is not a part here", key)
                continue
            yield Bearing(
                key=key,
                document=one.document.content_sha256,
                claim=one.finding.claim,
                caveat=one.finding.caveat,
                quote=one.finding.quote,
                locator=one.finding.locator,
                cited=one.document.cited(),
                why=placement.why,
            )


async def assign(
    manuscript: Manuscript,
    distillations: Iterable[Distillation],
    *,
    vocabulary: str = "",
) -> WorkFindings:
    """Place what the research establishes onto the parts it bears on.

    One call over the whole list rather than one per finding, because placing
    is comparative: a finding belongs to the part it fits *best*, and a pass
    shown one finding at a time has no way to notice that the part it wants is
    already carrying six.
    """
    held = sourced(distillations)
    if not held:
        return WorkFindings(vocabulary=vocabulary, assigned_at=now_stamp())

    placed = await query(
        f"{outlined(manuscript)}\n\n## Findings to place\n\n{listed(held)}",
        output_type=Placements,
        model=stage_model(ASSIGN_STAGE),
        system_prompt=ASSIGN_SYSTEM,
        autonomy="unattended",
        prefix="assign",
    )
    if placed is None:
        logger.warning("The assigning pass returned nothing for %s", manuscript.title)
        return WorkFindings(vocabulary=vocabulary, assigned_at=now_stamp())

    bearings = tuple(borne(manuscript, held, placed.placements))
    logger.info(
        "Placed %d of %d finding(s) of %s onto %d part(s)",
        len(bearings),
        len(held),
        manuscript.title,
        len(dict.fromkeys(one.key for one in bearings)),
    )
    return WorkFindings(
        bearings=bearings, vocabulary=vocabulary, assigned_at=now_stamp()
    )


def arrived(before: WorkFindings, after: WorkFindings) -> tuple[Bearing, ...]:
    """What the later assignment holds that the earlier one did not.

    The whole of what dirties a part. A finding already on a part when it was
    last built has been accounted for, and reporting it again would dirty that
    part on every sync — which is the failure that would make this unusable
    rather than merely wasteful.
    """
    standing = before.identities()
    return tuple(held for held in after.bearings if held.identity() not in standing)


def changes(arrivals: Iterable[Bearing]) -> tuple[ProposedChange, ...]:
    """The new bearings as the facts that reach the parts they landed on."""
    return tuple(held.change() for held in arrivals)


def briefing(found: WorkFindings, key: str) -> str:
    """What the research holds on one part, as its run is handed it.

    Handed to the run rather than searched for, on the same argument the corpus
    briefing already makes to the plan stage: a stage reaches for a search once
    it knows there is something to look for, and a part working from an older
    draft is exactly the case that does not know. These have been read in full
    already, so what arrives is what each document establishes rather than that
    it exists.
    """
    held = found.on(key)
    if not held:
        return ""

    def lines() -> Iterator[str]:
        """Each finding placed on this part, with its caveat and its source."""
        for one in held:
            caveated = f"\n  caveat: {one.caveat}" if one.caveat else ""
            quoted = f'\n  "{one.quote}"' if one.quote else ""
            placed = f" [{one.locator}]" if one.locator else ""
            yield f"- {one.claim}{caveated}{quoted}\n  {one.cited}{placed}"

    return (
        f"## What the research holds on this part\n\n{len(held)} finding(s), each "
        f"read out of the document named under it. The caveat is the document's "
        f"own authors' — a number stated without it is an overclaim they did not "
        f"make.\n\n" + "\n".join(lines()) + "\n"
    )
