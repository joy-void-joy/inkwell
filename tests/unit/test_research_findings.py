"""The corpus as an upstream node of the same build graph the book runs on.

Three arrows — distil, assign, dirty — each cached on what it is about. What is
worth pinning is that a document is read once ever and keyed on its bytes, that
placing findings reads titles rather than prose, that a sync which finds
nothing new dirties nothing (which is what makes the loop settle), and that the
half which moves parts is separable from the half that costs money.
"""

from pathlib import Path

import pytest

from inkwell.corpus.distillation import (
    DistilRequest,
    Distillation,
    Distiller,
    Finding,
    awaiting_distillation,
    by_content,
    distil_entries,
    findings_for,
    read_distillation,
)
from inkwell.corpus.retrieval import read_index
from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument
from inkwell.corpus.tags import DocumentTags
from inkwell.manuscript.facts import Dependency, bears_on
from inkwell.manuscript.findings import (
    Bearing,
    Placement,
    Placements,
    WorkFindings,
    arrived,
    borne,
    briefing,
    changes,
    outlined,
    runnable_keys,
    sourced,
)
from inkwell.manuscript.graph import adopted, readings, sweep
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.state import WorkState
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.1 Bio Risk

Prose.

## 2.3.2 Cyber Risk

Prose about cyber risk.
"""


def atlas_like(root: Path) -> Path:
    """A two-part work, small enough to read at once."""
    chapter = root / "chapters" / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    return root / "chapters"


def corpus_with(root: Path, *documents: StoredDocument) -> CorpusStore:
    """A corpus holding these documents, with a body on disk for each."""
    store = CorpusStore(root=root / "corpus")
    for document in documents:
        store.store_markdown("aisi", document.slug, f"# {document.title}\n\nProse.\n")
    store.save(
        SourceShard(
            source="aisi",
            documents=[
                one.model_copy(update={"filename": f"{one.slug}.md"})
                for one in documents
            ],
        )
    )
    return store


def paper(slug: str, digest: str, title: str = "A paper") -> StoredDocument:
    """One stored document, identified by the bytes it was fetched as."""
    return StoredDocument(
        slug=slug,
        url=f"https://fixture.test/{slug}",
        title=title,
        content_sha256=digest,
        tags=DocumentTags(core=("subject:security",), judged=True),
    )


class Counting(Distiller):
    """A distiller that records what it was asked and answers the same thing."""

    def __init__(self, *findings: Finding) -> None:
        self.findings = findings
        self.asked: list[str] = []

    async def distil(self, request: DistilRequest) -> tuple[Finding, ...]:
        self.asked.append(request.slug)
        return self.findings


class TestADocumentIsReadOnceEver:
    """The corpus de-duplicated documents; nothing de-duplicated conclusions.
    The next subsection reopened the same paper and re-derived the same numbers
    at full price."""

    @pytest.mark.asyncio
    async def test_a_document_already_read_is_not_read_again(
        self, tmp_path: Path
    ) -> None:
        store = corpus_with(tmp_path, paper("a", "digest-a"))
        entries = read_index(store).entries
        reader = Counting(Finding(claim="A doubles every five months."))

        await distil_entries(store, entries, reader)
        await distil_entries(store, entries, reader)

        assert reader.asked == ["a"]

    @pytest.mark.asyncio
    async def test_the_reading_is_kept_under_the_content_that_produced_it(
        self, tmp_path: Path
    ) -> None:
        """A document that changes source or gets re-slugged keeps what was read
        of it; two sources holding one paper read it once between them."""
        store = corpus_with(tmp_path, paper("a", "digest-a"))
        reader = Counting(Finding(claim="A doubles every five months."))

        await distil_entries(store, read_index(store).entries, reader)

        held = read_distillation(store, "digest-a")
        assert held is not None
        assert held.findings[0].claim == "A doubles every five months."

    def test_two_entries_of_one_content_are_priced_once(self, tmp_path: Path) -> None:
        store = corpus_with(tmp_path, paper("a", "shared"), paper("a-mirror", "shared"))

        assert len(by_content(read_index(store).entries)) == 1

    def test_an_unread_document_is_what_a_run_would_cost(self, tmp_path: Path) -> None:
        store = corpus_with(tmp_path, paper("a", "digest-a"))

        pending = awaiting_distillation(store, read_index(store).entries)

        assert [one.document.slug for one in pending] == ["a"]

    @pytest.mark.asyncio
    async def test_a_reading_that_failed_is_recorded_rather_than_left_absent(
        self, tmp_path: Path
    ) -> None:
        """So the next run can tell "this establishes nothing" from "try
        again", and a document that fails every time is not re-read forever."""
        store = corpus_with(tmp_path, paper("a", "digest-a"))

        class Refuses(Distiller):
            async def distil(self, request: DistilRequest) -> None:
                return None

        await distil_entries(store, read_index(store).entries, Refuses())

        held = read_distillation(store, "digest-a")
        assert held is not None and not held.read()

    @pytest.mark.asyncio
    async def test_a_failed_reading_is_not_offered_as_a_finding(
        self, tmp_path: Path
    ) -> None:
        store = corpus_with(tmp_path, paper("a", "digest-a"))

        class Refuses(Distiller):
            async def distil(self, request: DistilRequest) -> None:
                return None

        await distil_entries(store, read_index(store).entries, Refuses())

        assert findings_for(store, read_index(store).entries) == ()


class TestPlacingReadsTitlesRatherThanProse:
    """What a subsection titled "2.3.2 Cyber Risk" is about is answerable from
    its title, and answering it that way is what keeps a single part from
    triggering a book read."""

    def test_the_outline_names_every_part_by_the_key_state_uses(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        said = outlined(work)

        assert "02/03/2.3.2" in said
        assert "Prose about cyber risk" not in said

    def test_only_a_part_a_run_can_be_about_can_be_placed_on(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert "02/03/2.3.2" in runnable_keys(work)
        assert "02" not in runnable_keys(work)

    def test_a_key_the_pass_invented_places_the_finding_nowhere(
        self, tmp_path: Path
    ) -> None:
        """It would be a bearing on a part that does not exist, whose change
        fact nothing consumes — silent rather than wrong."""
        work = read_manuscript(atlas_like(tmp_path))
        held = sourced(
            (Distillation(content_sha256="d", findings=(Finding(claim="Something."),)),)
        )

        placed = tuple(
            borne(work, held, [Placement(finding=1, keys=["09/09/nowhere"])])
        )

        assert placed == ()

    def test_a_finding_number_nobody_asked_about_places_nothing(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = sourced(
            (Distillation(content_sha256="d", findings=(Finding(claim="Something."),)),)
        )

        assert (
            tuple(borne(work, held, [Placement(finding=7, keys=["02/03/2.3.2"])])) == ()
        )


class TestASyncThatFindsNothingNewDirtiesNothing:
    """Settling by construction. A finding already on a part when it was last
    built has been accounted for, and reporting it again would dirty that part
    on every sync."""

    def bearing(self, claim: str = "A doubles every five months.") -> Bearing:
        return Bearing(key="02/03/2.3.2", document="digest-a", claim=claim)

    def test_a_repeated_assignment_arrives_with_nothing(self) -> None:
        standing = WorkFindings(bearings=(self.bearing(),))

        assert arrived(standing, WorkFindings(bearings=(self.bearing(),))) == ()

    def test_a_reworded_reason_is_not_a_new_finding(self) -> None:
        """A reassignment reaching the same conclusion in different words has
        moved nothing."""
        standing = WorkFindings(bearings=(self.bearing(),))
        again = self.bearing().model_copy(update={"why": "phrased another way"})

        assert arrived(standing, WorkFindings(bearings=(again,))) == ()

    def test_a_new_claim_from_the_same_document_arrives(self) -> None:
        standing = WorkFindings(bearings=(self.bearing(),))
        more = WorkFindings(bearings=(self.bearing(), self.bearing("Also this.")))

        assert [one.claim for one in arrived(standing, more)] == ["Also this."]

    def test_the_first_sync_finds_everything_new(self) -> None:
        assert (
            len(arrived(WorkFindings(), WorkFindings(bearings=(self.bearing(),)))) == 1
        )


class TestAFindingReachesItsPartThroughTheOrdinarySweep:
    """The sweep gates and the planner assigns, never the reverse: a reader
    asked "does this need work?" twice answers differently twice, and a loop
    whose dirtiness signal is a judgement never settles."""

    def built(self, tmp_path: Path) -> tuple[Manuscript, WorkState]:
        """A work every part of which is stamped as built from what it holds."""
        work = read_manuscript(atlas_like(tmp_path))
        return work, adopted(WorkState(), readings(work))

    def test_a_part_depends_on_what_the_research_holds_about_it(
        self, tmp_path: Path
    ) -> None:
        """Derived rather than reported, so a run that read no research still
        hears about a paper published afterwards — which is exactly the part
        most in need of hearing."""
        work = read_manuscript(atlas_like(tmp_path))
        held = next(one for one in readings(work) if one.node.key == "02/03/2.3.2")

        assert held.consumed.touches(bears_on("02/03/2.3.2"))

    def test_a_settled_work_stays_settled_until_something_lands(
        self, tmp_path: Path
    ) -> None:
        work, state = self.built(tmp_path)

        assert sweep(state, readings(work)).settled()

    def test_a_finding_landing_on_a_part_puts_that_part_out_of_date(
        self, tmp_path: Path
    ) -> None:
        work, state = self.built(tmp_path)
        landed = Bearing(
            key="02/03/2.3.2",
            document="digest-a",
            claim="Cyber-range performance doubles every five months.",
            cited="AISI evaluations — https://fixture.test/a",
        )

        moved = state.changed("corpus", changes((landed,)))
        found = sweep(moved, readings(work))

        assert [one.key for one in found.dirty()] == ["02/03/2.3.2"]

    def test_the_part_is_told_which_paper_moved_it(self, tmp_path: Path) -> None:
        work, state = self.built(tmp_path)
        landed = Bearing(
            key="02/03/2.3.2",
            document="digest-a",
            claim="Cyber-range performance doubles every five months.",
            cited="AISI evaluations — https://fixture.test/a",
        )

        moved = state.changed("corpus", changes((landed,)))
        verdict = sweep(moved, readings(work)).dirty()[0]

        assert verdict.staleness == "upstream"
        assert "AISI evaluations" in " ".join(verdict.reasons)

    def test_a_finding_on_one_part_leaves_its_sibling_alone(
        self, tmp_path: Path
    ) -> None:
        work, state = self.built(tmp_path)
        landed = Bearing(key="02/03/2.3.2", document="d", claim="Something.")

        moved = state.changed("corpus", changes((landed,)))

        assert "02/03/2.3.1" not in [
            one.key for one in sweep(moved, readings(work)).dirty()
        ]


class TestTheRunIsHandedWhatWasFoundForIt:
    """Handed rather than searched for, on the argument the corpus briefing
    already makes to the plan stage: a stage reaches for a search once it knows
    there is something to look for."""

    def test_the_briefing_carries_the_claim_and_its_caveat(self) -> None:
        """A number stated without the caveat is an overclaim its own authors
        did not make."""
        found = WorkFindings(
            bearings=(
                Bearing(
                    key="02/03/2.3.2",
                    document="d",
                    claim="Cyber-range performance doubles every five months.",
                    caveat="over the evaluated period only",
                    cited="AISI — https://fixture.test/a",
                ),
            )
        )

        said = briefing(found, "02/03/2.3.2")

        assert "doubles every five months" in said
        assert "over the evaluated period only" in said
        assert "AISI" in said

    def test_a_part_nothing_was_placed_on_is_told_nothing(self) -> None:
        assert briefing(WorkFindings(), "02/03/2.3.2") == ""


class TestWhereAnAssignmentIsKept:
    """Derived, and stored anyway: what dirties a part is the difference
    between this assignment and the last one, and a difference needs the last
    one."""

    def test_a_work_nothing_has_synced_holds_nothing(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")

        assert store.load_findings("atlas").bearings == ()

    def test_what_was_placed_survives_the_run_that_placed_it(
        self, tmp_path: Path
    ) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        found = WorkFindings(
            bearings=(Bearing(key="02/03/2.3.2", document="d", claim="Something."),),
            vocabulary="79fafa35a1c1",
        )

        store.publish_findings("atlas", found)

        assert store.load_findings("atlas").vocabulary == "79fafa35a1c1"


class TestTheFourthKindOfDependence:
    """It is the one that does not come from another part."""

    def test_it_is_keyed_on_the_part_rather_than_the_document(self) -> None:
        """A part cannot have consumed a document nobody had read when it last
        ran, and a paper published since is precisely the news it needs."""
        assert bears_on("02/03/2.3.2") == Dependency(
            kind="finding", subject="02/03/2.3.2"
        )

    def test_it_reads_as_a_sentence_a_part_is_told(self) -> None:
        assert bears_on("02/03/2.3.2").render() == (
            "what the research holds on '02/03/2.3.2'"
        )

    def test_a_placement_becomes_the_fact_that_reaches_its_part(self) -> None:
        landed = Bearing(key="02/03/2.3.2", document="d", claim="Something.")

        assert changes((landed,))[0].dependency == bears_on("02/03/2.3.2")


class TestPlacementsCarryOnlyWhatWasAsked:
    """A pass shown one finding at a time cannot notice that the part it wants
    is already carrying six, so placing is one call over the whole list."""

    def test_every_finding_of_every_document_is_offered(self) -> None:
        held = sourced(
            (
                Distillation(
                    content_sha256="a",
                    findings=(Finding(claim="One."), Finding(claim="Two.")),
                ),
                Distillation(content_sha256="b", findings=(Finding(claim="Three."),)),
            )
        )

        assert [one.finding.claim for one in held] == ["One.", "Two.", "Three."]

    def test_a_document_with_no_findings_offers_none(self) -> None:
        assert sourced((Distillation(content_sha256="a"),)) == ()


class TestOnlyPlacementsThatLandAreRecorded:
    """A ledger full of facts addressed to nobody is worse than a dropped
    placement, because nothing ever notices it."""

    def test_a_placement_naming_a_real_part_is_kept(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = sourced(
            (
                Distillation(
                    content_sha256="d",
                    title="A paper",
                    url="https://fixture.test/a",
                    findings=(Finding(claim="Something.", caveat="in one setting"),),
                ),
            )
        )

        placed = tuple(
            borne(
                work,
                held,
                Placements(
                    placements=[Placement(finding=1, keys=["02/03/2.3.2"], why="on it")]
                ).placements,
            )
        )

        assert [one.key for one in placed] == ["02/03/2.3.2"]
        assert placed[0].caveat == "in one setting"
        assert placed[0].cited == "A paper — https://fixture.test/a"

    def test_a_node_that_only_contains_others_is_refused(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = sourced(
            (Distillation(content_sha256="d", findings=(Finding(claim="X."),)),)
        )

        assert tuple(borne(work, held, [Placement(finding=1, keys=["02"])])) == ()


def test_a_node_with_no_children_and_no_path_is_not_placeable() -> None:
    """A group holds parts without being one, so nothing runs against it and
    nothing should be placed on it either."""
    work = Manuscript(
        title="W",
        root="/nowhere",
        children=[ManuscriptNode(key="01", kind="group", title="Appendix")],
    )

    assert runnable_keys(work) == ()
