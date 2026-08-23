"""Where a part's questions go, and what happens to a lease nobody honours.

Two things the report of one bad run named. A part run's assumptions were posted
as comments on a document one run created and nobody would open again — two
hundred parts, two hundred documents, and every question filed where nobody is
looking. And a run whose process ended between the pipeline returning and the
splice left its part reading `running`, with every surface reporting a rewrite
in progress for as long as anybody left it.
"""

from datetime import timedelta
from pathlib import Path

from lup.channels.models import utc_now

from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.mailbox import (
    ancestors_of,
    common_ancestor,
    concerns,
    escalated_to,
)
from inkwell.manuscript.state import LEASE_TERM, NodeRecord, WorkState

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

OTHER_PAGES = """\
nav:
- 4.1 - Evaluations: 01.md
title: 04 - Evaluations
"""

EVALUATIONS = """\
# 4.1 Evaluations

## 4.1.1 Cyber Ranges

Prose.
"""


def two_chapters(root: Path) -> Path:
    """A work with parts in two chapters, so a question can span them."""
    chapters = root / "chapters"
    misuse = chapters / "02"
    misuse.mkdir(parents=True)
    (misuse / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (misuse / "03.md").write_text(MISUSE, encoding="utf-8")
    evaluations = chapters / "04"
    evaluations.mkdir(parents=True)
    (evaluations / ".pages.yml").write_text(OTHER_PAGES, encoding="utf-8")
    (evaluations / "01.md").write_text(EVALUATIONS, encoding="utf-8")
    return chapters


class TestAQuestionGoesToTheLevelItIsAbout:
    """A question that names other parts is not a question about this one, and
    addressing it to the section holding only this one means it waits at a
    level that has to escalate it again by hand."""

    def test_a_question_about_this_part_alone_goes_to_its_section(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert escalated_to(
            work, "02/03/2.3.2", "Should this cite the 2024 paper?"
        ) == ("02/03")

    def test_a_question_naming_a_sibling_still_goes_to_their_section(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        held = escalated_to(
            work, "02/03/2.3.2", "Does 2.3.1 Bio Risk already define this?"
        )

        assert held == "02/03"

    def test_a_question_naming_another_chapter_goes_to_the_work(
        self, tmp_path: Path
    ) -> None:
        """Two parts in different chapters have only the work above them, and
        the section holding one of them cannot answer for both."""
        work = read_manuscript(two_chapters(tmp_path))

        held = escalated_to(
            work,
            "02/03/2.3.2",
            "Should this and 4.1.1 Cyber Ranges both define the term?",
        )

        assert held == ""

    def test_a_question_naming_nothing_escalates_one_level(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert escalated_to(work, "02/03/2.3.2") == "02/03"

    def test_a_part_with_no_ancestor_addresses_the_work(self, tmp_path: Path) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert escalated_to(work, "02") == ""

    def test_the_parts_a_question_names_are_found_by_title_or_key(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert "02/03/2.3.1" in concerns(work, "Does 2.3.1 Bio Risk cover it?")
        assert "02/03/2.3.1" in concerns(work, "Does 02/03/2.3.1 cover it?")

    def test_a_question_naming_no_part_concerns_none(self, tmp_path: Path) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert concerns(work, "Is this still true in 2026?") == ()


class TestReadingTheTreeUpwards:
    """A key is already a path, so what is above a part is read off it."""

    def test_every_level_above_a_part_is_named_nearest_last(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert ancestors_of(work, "02/03/2.3.2") == ("02", "02/03")

    def test_the_nearest_part_holding_two_siblings_is_their_section(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        held = common_ancestor(work, ["02/03/2.3.1", "02/03/2.3.2"])

        assert held == "02/03"

    def test_the_nearest_part_holding_two_chapters_is_the_work(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert common_ancestor(work, ["02/03/2.3.2", "04/01/4.1.1"]) == ""

    def test_one_part_holds_itself(self, tmp_path: Path) -> None:
        work = read_manuscript(two_chapters(tmp_path))

        assert common_ancestor(work, ["02/03/2.3.2"]) == "02/03/2.3.2"


class TestALeaseNobodyIsHonouring:
    """A run whose process ended left its part reading `running`, its work
    byte-identical to what it was handed, and every surface reporting a rewrite
    in progress."""

    def held(self, ago: timedelta) -> NodeRecord:
        """A part leased this long ago and never given back."""
        return NodeRecord(
            key="02/03/2.3.2",
            standing="running",
            holder="be3340ea9eab4b85",
            changed_at=utc_now() - ago,
        )

    def test_a_lease_taken_just_now_is_honoured(self) -> None:
        assert not self.held(timedelta(minutes=5)).abandoned()

    def test_a_run_longer_than_the_longest_measured_one_is_still_honoured(
        self,
    ) -> None:
        """The longest measured part run took five hours, and being wrong here
        means calling a working run abandoned."""
        assert not self.held(timedelta(hours=6)).abandoned()

    def test_a_lease_past_its_term_is_reported(self) -> None:
        assert self.held(LEASE_TERM + timedelta(minutes=1)).abandoned()

    def test_a_part_that_is_not_running_is_never_abandoned(self) -> None:
        """A stamp from last March is a part built last March, not a lease
        nobody honoured."""
        old = NodeRecord(
            key="02/03/2.3.2",
            standing="parked",
            changed_at=utc_now() - timedelta(days=90),
        )

        assert not old.abandoned()

    def test_a_work_reports_which_of_its_leases_have_lapsed(self) -> None:
        state = WorkState(
            records=(
                self.held(LEASE_TERM + timedelta(hours=1)),
                NodeRecord(key="02/03/2.3.1", standing="running", holder="fresh"),
            )
        )

        assert [one.key for one in state.abandoned()] == ["02/03/2.3.2"]

    def test_nothing_reclaims_it(self) -> None:
        """Whether the run is gone or merely slow is not something another
        process can tell, and taking the lease from one still writing is the
        failure the lease exists to prevent."""
        state = WorkState(records=(self.held(LEASE_TERM + timedelta(days=7)),))

        assert state.standing("02/03/2.3.2") == "running"
