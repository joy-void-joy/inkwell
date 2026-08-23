"""What was established about one cited URL, kept under the URL.

A book cites the same pages over and over, so checking per citation pays
repeatedly to reach the same answer. What is worth pinning is that a reference
is opened once ever, that two spellings of one page share a verdict, that the
check is about the *reference* rather than the claim, and that a reference which
does not hold up reaches the parts citing it through the machinery findings
already use rather than a channel of its own.
"""

import asyncio
from pathlib import Path

import pytest

from inkwell.agent.references import (
    ReferenceReader,
    ReferenceStep,
    ReferenceVerdict,
    VerdictStore,
    canonical,
    check_references,
    distinct,
    keyed,
    unchecked,
    unsound,
    verdicts_for,
)
from inkwell.manuscript.findings import Bearing, WorkFindings
from inkwell.manuscript.graph import adopted, readings, sweep
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.research import (
    cited_by_part,
    faulted,
    publish_references,
    sweep_references,
)
from inkwell.manuscript.state import WorkState
from inkwell.manuscript.store import ManuscriptStore

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.1 Bio Risk

Prose citing [a paper](https://fixture.test/bio).

## 2.3.2 Cyber Risk

Prose citing [an outage report](https://fixture.test/outage) and
[the same paper](https://fixture.test/bio).
"""


def atlas_like(root: Path) -> Path:
    """A two-part work whose parts share one citation."""
    chapter = root / "chapters" / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    return root / "chapters"


class Opens(ReferenceReader):
    """A reader that answers from a fixture and counts what it was asked."""

    def __init__(self, **verdicts: ReferenceVerdict) -> None:
        self.verdicts = verdicts
        self.asked: list[str] = []

    async def check(self, url: str) -> ReferenceVerdict | None:
        self.asked.append(url)
        return self.verdicts.get(url) or ReferenceVerdict(
            url=url, canonical_url=canonical(url), reachable=True, title="Something"
        )


def dead(url: str) -> ReferenceVerdict:
    """A verdict for a reference with nothing behind it."""
    return ReferenceVerdict(url=url, canonical_url=canonical(url), reachable=False)


def doubted(url: str, note: str) -> ReferenceVerdict:
    """A verdict for a reference that resolves and is not what it seems."""
    return ReferenceVerdict(
        url=url,
        canonical_url=canonical(url),
        reachable=True,
        title="A different report",
        note=note,
    )


class TestTwoSpellingsOfOnePageShareAVerdict:
    """Parsed rather than trimmed: a fragment is a position within a document
    and never a different document."""

    def test_a_fragment_names_the_same_reference(self) -> None:
        assert canonical("https://a.test/p#s2") == canonical("https://a.test/p")

    def test_a_trailing_slash_names_the_same_reference(self) -> None:
        assert canonical("https://a.test/p/") == canonical("https://a.test/p")

    def test_case_in_the_host_does_not_make_a_new_reference(self) -> None:
        assert canonical("https://A.TEST/p") == canonical("https://a.test/p")

    def test_a_query_string_does_make_one(self) -> None:
        """For a great many sites the query string *is* the document."""
        assert canonical("https://a.test/p?id=2") != canonical("https://a.test/p")

    def test_two_spellings_land_in_one_file(self) -> None:
        assert keyed("https://a.test/p/") == keyed("https://a.test/p#s2")

    def test_each_reference_is_counted_once(self) -> None:
        held = distinct(["https://a.test/p", "https://a.test/p/", "https://b.test/q"])

        assert len(held) == 2


class TestAReferenceIsOpenedOnceEver:
    """One page carries 27 of the Atlas's citations; opening it per citation
    pays 27 times to reach the same answer."""

    @pytest.mark.asyncio
    async def test_a_reference_two_parts_cite_is_opened_once(
        self, tmp_path: Path
    ) -> None:
        store = VerdictStore(root=tmp_path / "references")
        reader = Opens()

        await check_references(
            store,
            ["https://a.test/p", "https://a.test/p", "https://b.test/q"],
            reader,
        )

        assert len(reader.asked) == 2

    @pytest.mark.asyncio
    async def test_a_second_run_opens_nothing_already_checked(
        self, tmp_path: Path
    ) -> None:
        store = VerdictStore(root=tmp_path / "references")
        reader = Opens()

        await check_references(store, ["https://a.test/p"], reader)
        await check_references(store, ["https://a.test/p"], reader)

        assert reader.asked == [canonical("https://a.test/p")]

    def test_what_a_run_would_cost_is_knowable_before_it_runs(
        self, tmp_path: Path
    ) -> None:
        store = VerdictStore(root=tmp_path / "references")
        store.save(dead("https://a.test/p"))

        assert unchecked(store, ["https://a.test/p", "https://b.test/q"]) == (
            canonical("https://b.test/q"),
        )

    @pytest.mark.asyncio
    async def test_a_check_that_could_not_be_made_records_nothing(
        self, tmp_path: Path
    ) -> None:
        """Unlike a document that establishes nothing, a reference the network
        refused today is one to try again rather than one to record as dead."""
        store = VerdictStore(root=tmp_path / "references")

        class Refuses(ReferenceReader):
            async def check(self, url: str) -> None:
                return None

        steps: list[ReferenceStep] = []
        report = await check_references(
            store,
            ["https://a.test/p"],
            Refuses(),
            progress=steps.append,
        )

        assert report.failed == 1
        assert store.load("https://a.test/p") is None
        assert steps[0].failure == "the check did not come back"

    @pytest.mark.asyncio
    async def test_an_interrupted_check_closes_what_it_opened(
        self, tmp_path: Path
    ) -> None:
        """Cancellation must not leave the activity view claiming that a
        reader is still working forever."""
        entered = asyncio.Event()
        steps: list[ReferenceStep] = []

        class Waits(ReferenceReader):
            async def check(self, url: str) -> None:
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(
            check_references(
                VerdictStore(root=tmp_path / "references"),
                ["https://a.test/p"],
                Waits(),
                progress=steps.append,
            )
        )
        await entered.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert [one.failure for one in steps] == ["cancelled"]

    @pytest.mark.asyncio
    async def test_the_report_separates_dead_from_doubted(self, tmp_path: Path) -> None:
        """A reference that is wrong wants the citation corrected; one that is
        gone wants a replacement. Reporting both as a problem leaves whoever
        reads it doing the triage again."""
        store = VerdictStore(root=tmp_path / "references")
        reader = Opens(
            **{
                canonical("https://a.test/p"): dead("https://a.test/p"),
                canonical("https://b.test/q"): doubted(
                    "https://b.test/q", "this is a listing, not the report"
                ),
            }
        )

        report = await check_references(
            store, ["https://a.test/p", "https://b.test/q"], reader
        )

        assert (report.dead, report.doubted) == (1, 1)


class TestWhatCountsAsUnsound:
    """A reference a book can ship resolves and is what it says it is."""

    def test_a_reference_that_resolves_and_reads_right_is_sound(self) -> None:
        held = ReferenceVerdict(url="https://a.test/p", reachable=True, title="A paper")

        assert held.sound()

    def test_a_sound_reference_may_still_carry_a_caveat(self) -> None:
        held = ReferenceVerdict(
            url="https://a.test/p",
            reachable=True,
            title="A preprint",
            caveat="not peer reviewed",
        )

        assert held.sound()
        assert "caveat: not peer reviewed" in held.render()

    def test_a_dead_reference_is_not(self) -> None:
        assert not dead("https://a.test/p").sound()

    def test_one_that_resolves_to_the_wrong_thing_is_not(self) -> None:
        assert not doubted("https://a.test/p", "a listing").sound()

    def test_only_the_unsound_are_reported(self) -> None:
        held = (
            ReferenceVerdict(url="https://a.test/p", reachable=True),
            dead("https://b.test/q"),
        )

        assert [one.url for one in unsound(held)] == ["https://b.test/q"]


class TestWhichPartCitesWhat:
    """Read off the prose, because a citation is in the text and the text is
    what ships — a record of what a run intended to cite would miss the one an
    author pasted in by hand."""

    def test_each_part_carries_its_own_citations(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        held = cited_by_part(work)

        assert held["02/03/2.3.2"] == (
            "https://fixture.test/outage",
            "https://fixture.test/bio",
        )

    def test_a_reference_two_parts_share_is_on_both(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        held = cited_by_part(work)

        assert "https://fixture.test/bio" in held["02/03/2.3.1"]
        assert "https://fixture.test/bio" in held["02/03/2.3.2"]


class TestABadReferenceReachesThePartsThatCiteIt:
    """A bearing rather than a channel of its own: it is the same kind of thing
    a finding is, so it dirties the part through the sweep and reaches the next
    run's brief with nothing new anywhere."""

    def test_it_is_placed_on_the_part_carrying_it(self) -> None:
        held = faulted(dead("https://a.test/p"), "02/03/2.3.2")

        assert held.key == "02/03/2.3.2"
        assert "does not hold up" in held.claim

    def test_it_names_the_source_a_writer_would_look_for(self) -> None:
        held = faulted(doubted("https://a.test/p", "a listing"), "02/03/2.3.2")

        assert held.cited == "https://a.test/p"
        assert "a listing" in held.claim

    @pytest.mark.asyncio
    async def test_a_work_whose_references_hold_up_dirties_nothing(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        store = ManuscriptStore(root=tmp_path / "manuscripts")

        swept = await sweep_references(
            store,
            VerdictStore(root=tmp_path / "references"),
            "atlas",
            work,
            reader=Opens(),
        )

        assert swept.arrivals == ()

    @pytest.mark.asyncio
    async def test_a_shared_dead_reference_dirties_both_parts_that_cite_it(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        reader = Opens(
            **{canonical("https://fixture.test/bio"): dead("https://fixture.test/bio")}
        )

        swept = await sweep_references(
            store,
            VerdictStore(root=tmp_path / "references"),
            "atlas",
            work,
            reader=reader,
        )

        assert set(swept.dirtied()) == {"02/03/2.3.1", "02/03/2.3.2"}

    @pytest.mark.asyncio
    async def test_publishing_puts_those_parts_out_of_date(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_state("atlas", adopted(WorkState(), readings(work)))
        reader = Opens(
            **{
                canonical("https://fixture.test/outage"): dead(
                    "https://fixture.test/outage"
                )
            }
        )

        swept = await sweep_references(
            store,
            VerdictStore(root=tmp_path / "references"),
            "atlas",
            work,
            reader=reader,
        )
        publish_references(store, swept)

        found = sweep(store.load_state("atlas"), readings(work))
        assert [one.key for one in found.dirty()] == ["02/03/2.3.2"]

    @pytest.mark.asyncio
    async def test_publishing_keeps_what_the_research_already_placed(
        self, tmp_path: Path
    ) -> None:
        """The two are the same kind of news about the same parts, so a sweep
        that wrote its own set whole would silently retract every finding the
        corpus placed."""
        work = read_manuscript(atlas_like(tmp_path))
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_findings(
            "atlas",
            WorkFindings(
                bearings=(Bearing(key="02/03/2.3.1", document="d", claim="A finding."),)
            ),
        )
        reader = Opens(
            **{
                canonical("https://fixture.test/outage"): dead(
                    "https://fixture.test/outage"
                )
            }
        )

        swept = await sweep_references(
            store,
            VerdictStore(root=tmp_path / "references"),
            "atlas",
            work,
            reader=reader,
        )
        publish_references(store, swept)

        claims = [one.claim for one in store.load_findings("atlas").bearings]
        assert "A finding." in claims
        assert any("does not hold up" in one for one in claims)

    @pytest.mark.asyncio
    async def test_a_second_sweep_finding_the_same_fault_dirties_nothing_again(
        self, tmp_path: Path
    ) -> None:
        """Otherwise every sweep re-dirties every part with a dead link, and the
        loop never settles."""
        work = read_manuscript(atlas_like(tmp_path))
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        verdicts = VerdictStore(root=tmp_path / "references")
        reader = Opens(
            **{
                canonical("https://fixture.test/outage"): dead(
                    "https://fixture.test/outage"
                )
            }
        )

        publish_references(
            store,
            await sweep_references(store, verdicts, "atlas", work, reader=reader),
        )
        again = await sweep_references(store, verdicts, "atlas", work, reader=reader)

        assert again.arrivals == ()


class TestTheStoreIsKeyedOnTheUrl:
    """A URL is not a filename, so the file is named by a digest and the URL
    itself is recorded inside."""

    def test_what_was_established_survives_the_run_that_established_it(
        self, tmp_path: Path
    ) -> None:
        store = VerdictStore(root=tmp_path / "references")
        store.save(doubted("https://a.test/p", "a listing"))

        held = store.load("https://a.test/p/")
        assert held is not None
        assert held.note == "a listing"

    def test_an_unchecked_reference_reads_as_unchecked(self, tmp_path: Path) -> None:
        assert (
            VerdictStore(root=tmp_path / "references").load("https://a.test/p") is None
        )

    def test_only_checked_references_come_back(self, tmp_path: Path) -> None:
        store = VerdictStore(root=tmp_path / "references")
        store.save(dead("https://a.test/p"))

        held = verdicts_for(store, ["https://a.test/p", "https://b.test/q"])

        assert [one.url for one in held] == ["https://a.test/p"]
