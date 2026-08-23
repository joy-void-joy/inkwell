"""Three levels where there was one, and a history that counts the failures.

A pass over a book reports when it is over. A part reports through the work's
state while it holds a lease. Beneath that, the agents doing the work reported
nowhere anything outside the run could read — and the agents that belong to no
part at all reported nowhere at all, so a pass spending hours reading documents
before it wrote a word looked idle.

The history has its own defect and the same shape: a build stamp names the run
that built a part, so a run that produced prose is findable and a run that
produced nothing is findable from nothing — which makes the runs most worth
opening the ones with the weakest linkage.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from lup.channels.models import utc_now

from inkwell.agent.client import AgentUpdate
from inkwell.environment.web.routes import works
from inkwell.environment.web.models import AgentEvent, ProgressEvent
from inkwell.environment.web.session_manager import SessionManager
from inkwell.agent.references import ReferenceStep
from inkwell.corpus.distillation import DistilStep
from inkwell.manuscript.attending import Attendance, WorkAgent, attending
from inkwell.manuscript.inheritance import ADOPTED_FILE
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.recording import import_work
from inkwell.manuscript.runner import PRODUCED_FILE, PartRun, opened, runs_under
from inkwell.manuscript.research import (
    distil_started,
    distil_watch,
    reference_started,
    reference_watch,
)
from inkwell.manuscript.store import ManuscriptStore

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.2 Cyber Risk

Prose about cyber risk.
"""


def atlas_like(root: Path) -> Path:
    """A one-part work, small enough to read at once."""
    chapter = root / "chapters" / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    return root / "chapters"


class TestTheAgentsThatBelongToNoPart:
    """Distilling a document, placing findings, checking a reference. A pass
    that spends hours reading before it writes looks idle without these, which
    is the state somebody stops a loop out of."""

    def test_an_agent_that_has_not_closed_is_running(self) -> None:
        held = WorkAgent(id="distil:a", kind="distil", about="A paper")

        assert held.running()

    def test_closing_one_marks_it_finished(self) -> None:
        held = Attendance().with_agent(WorkAgent(id="distil:a", kind="distil"))

        after = held.closing("distil:a", detail="3 finding(s)")

        assert not after.agents[0].running()
        assert after.agents[0].detail == "3 finding(s)"

    def test_an_agent_opened_twice_is_one_row(self) -> None:
        """A re-run after a failure is one row rather than a history nobody
        asked for."""
        held = Attendance().with_agent(WorkAgent(id="distil:a", kind="distil"))

        again = held.with_agent(WorkAgent(id="distil:a", kind="distil", about="A"))

        assert len(again.agents) == 1
        assert again.agents[0].about == "A"

    def test_the_order_agents_opened_in_is_kept(self) -> None:
        held = (
            Attendance()
            .with_agent(WorkAgent(id="a", kind="distil"))
            .with_agent(WorkAgent(id="b", kind="distil"))
            .with_agent(WorkAgent(id="a", kind="distil", about="revised"))
        )

        assert [one.id for one in held.agents] == ["a", "b"]

    def test_closing_one_nothing_opened_is_ignored(self) -> None:
        """A close that arrives for an agent nobody wrote down is a lost open,
        not a reason to fail the pass that did the work."""
        assert Attendance().closing("nobody").agents == ()

    def test_only_the_running_ones_are_live(self) -> None:
        held = (
            Attendance()
            .with_agent(WorkAgent(id="a", kind="distil"))
            .with_agent(WorkAgent(id="b", kind="distil"))
            .closing("a")
        )

        assert [one.id for one in held.live()] == ["b"]

    def test_the_record_is_read_by_a_process_that_is_not_the_pass(
        self, tmp_path: Path
    ) -> None:
        """The question is asked from a browser in another process, and an
        answer only the pass holds cannot be given."""
        log = attending(tmp_path / "atlas")
        log.opening(WorkAgent(id="distil:a", kind="distil", about="A paper"))
        log.closed("distil:a", detail="3 finding(s)")

        held = attending(tmp_path / "atlas").load()

        assert held.agents[0].detail == "3 finding(s)"
        assert not held.agents[0].running()

    def test_an_unwritten_record_reads_as_nothing_happening(
        self, tmp_path: Path
    ) -> None:
        assert attending(tmp_path / "atlas").load().agents == ()

    def test_the_store_answers_where_a_works_record_is(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.attending("atlas").opening(WorkAgent(id="a", kind="distil"))

        assert store.attending("atlas").load().agents[0].id == "a"

    def test_a_distiller_is_visible_while_its_model_turn_is_running(
        self, tmp_path: Path
    ) -> None:
        log = attending(tmp_path / "atlas")
        step = DistilStep(digest="abc", title="A paper", findings=0, done=0, total=3)

        distil_started(log)(step)

        assert [one.id for one in log.load().live()] == ["distil:abc"]

        distil_watch(log)(step.model_copy(update={"findings": 2, "done": 1}))
        held = log.load().agents[0]
        assert not held.running()
        assert held.detail == "2 finding(s) — 1 of 3"

    def test_a_reference_is_visible_while_it_is_being_checked(
        self, tmp_path: Path
    ) -> None:
        log = attending(tmp_path / "atlas")
        step = ReferenceStep(url="https://example.test/p", sound=False, done=0, total=4)

        reference_started(log)(step)

        assert [one.id for one in log.load().live()] == [
            "reference:https://example.test/p"
        ]

        reference_watch(log)(step.model_copy(update={"sound": True, "done": 1}))
        held = log.load().agents[0]
        assert not held.running()
        assert held.detail == "holds up — 1 of 4"

    @pytest.mark.parametrize(
        ("step", "started", "watch", "identifier"),
        [
            (
                DistilStep(
                    digest="abc",
                    title="A paper",
                    findings=0,
                    done=0,
                    total=3,
                ),
                distil_started,
                distil_watch,
                "distil:abc",
            ),
            (
                ReferenceStep(
                    url="https://example.test/p", sound=False, done=0, total=4
                ),
                reference_started,
                reference_watch,
                "reference:https://example.test/p",
            ),
        ],
    )
    def test_an_interrupted_reader_is_closed_as_cancelled(
        self,
        tmp_path: Path,
        step: DistilStep | ReferenceStep,
        started: object,
        watch: object,
        identifier: str,
    ) -> None:
        log = attending(tmp_path / "atlas")
        started(log)(step)  # type: ignore[operator]
        watch(log)(step.model_copy(update={"failure": "cancelled"}))  # type: ignore[operator]

        held = log.load().agents[0]
        assert held.id == identifier
        assert not held.running()
        assert held.failure == "cancelled"


class TestARunSaysWhoseItIs:
    """Everything else in a run's room is an output. This is the only thing
    that says whose run it was, and it is written first."""

    def test_a_run_records_its_part_before_it_does_anything(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        room = tmp_path / "runs" / "s1"
        room.mkdir(parents=True)

        opened(room, "atlas", node, "s1", ("something changed",))

        held = runs_under(tmp_path / "runs")
        assert [one.key for one in held] == ["02/03/2.3.2"]
        assert held[0].reasons == ("something changed",)

    def test_a_run_that_produced_nothing_is_still_found(self, tmp_path: Path) -> None:
        """A history read from build stamps is a history of successes with
        every failure missing, and the failures are worth opening."""
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        for session in ("s1", "s2"):
            room = tmp_path / "runs" / session
            room.mkdir(parents=True)
            opened(room, "atlas", node, session, ())
        (tmp_path / "runs" / "s2" / PRODUCED_FILE).write_text("prose", encoding="utf-8")

        assert len(runs_under(tmp_path / "runs")) == 2

    def test_runs_come_back_newest_first(self, tmp_path: Path) -> None:
        for session, ago in (("old", 2), ("new", 0)):
            room = tmp_path / "runs" / session
            room.mkdir(parents=True)
            (room / "run.json").write_text(
                PartRun(
                    session=session,
                    work="atlas",
                    key="02/03/2.3.2",
                    opened_at=utc_now() - timedelta(hours=ago),
                ).model_dump_json(),
                encoding="utf-8",
            )

        assert [one.session for one in runs_under(tmp_path / "runs")] == ["new", "old"]

    def test_an_unreadable_room_is_skipped_rather_than_fatal(
        self, tmp_path: Path
    ) -> None:
        room = tmp_path / "runs" / "broken"
        room.mkdir(parents=True)
        (room / "run.json").write_text("not json", encoding="utf-8")

        assert runs_under(tmp_path / "runs") == ()


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManuscriptStore:
    """A work imported and reachable through the routes."""
    store = ManuscriptStore(root=tmp_path / "manuscripts")
    import_work(store, "atlas", atlas_like(tmp_path), title="Atlas")
    monkeypatch.setattr(works, "manuscript_store", lambda: store)
    return store


def ran(store: ManuscriptStore, session: str, *, produced: bool = False) -> Path:
    """A room recording one run against the work's only part."""
    tree = store.load_tree("atlas")
    assert tree is not None
    node = tree.node("02/03/2.3.2")
    assert node is not None
    room = store.work_dir("atlas") / "runs" / session
    room.mkdir(parents=True)
    opened(room, "atlas", node, session, ())
    if produced:
        (room / PRODUCED_FILE).write_text("prose", encoding="utf-8")
    return room


class TestTheThreeLevelsAreOneReply:
    """Split across three calls, a page renders a pass with no parts and then
    parts with no agents, and the moment somebody is asking about is the moment
    those disagree."""

    def test_a_quiet_work_reports_quiet_at_every_level(
        self, recorded: ManuscriptStore
    ) -> None:
        held = works.activity("atlas")

        assert held.parts == ()
        assert held.working == ()
        assert not held.busy()

    def test_the_work_level_agents_reach_the_reply(
        self, recorded: ManuscriptStore
    ) -> None:
        recorded.attending("atlas").opening(
            WorkAgent(id="distil:a", kind="distil", about="A paper")
        )

        held = works.activity("atlas")

        assert [one.about for one in held.working] == ["A paper"]

    def test_one_shot_agents_reach_the_part_activity_roster(self) -> None:
        sessions = SessionManager()
        handle = sessions.adopt("s")
        started = AgentUpdate(label="brief", address="brief")
        handle.events = [
            AgentEvent(agent=started),
            AgentEvent(agent=started.model_copy(update={"status": "completed"})),
            AgentEvent(agent=AgentUpdate(label="inherit", address="inherit")),
        ]

        held = works.agents_of("s", sessions)

        assert [(one.address, one.running) for one in held] == [
            ("brief", False),
            ("inherit", True),
        ]

    def test_a_live_run_detail_keeps_its_complete_event_history(self) -> None:
        sessions = SessionManager()
        handle = sessions.adopt("s")
        handle.events = [ProgressEvent(message=str(index)) for index in range(250)]

        detail = sessions.get_session_detail("s")

        assert detail is not None
        assert len(detail.events) == 250

    def test_a_pass_reading_documents_does_not_look_idle(
        self, recorded: ManuscriptStore
    ) -> None:
        """The whole point: a pass spending hours reading before it writes a
        word held no lease on any part and reported nothing."""
        recorded.attending("atlas").opening(WorkAgent(id="distil:a", kind="distil"))

        assert works.activity("atlas").busy()

    def test_a_finished_agent_is_kept_but_stops_counting_as_busy(
        self, recorded: ManuscriptStore
    ) -> None:
        recorded.attending("atlas").opening(WorkAgent(id="distil:a", kind="distil"))
        recorded.attending("atlas").closed("distil:a", detail="3 finding(s)")

        held = works.activity("atlas")

        assert len(held.working) == 1
        assert not held.busy()

    def test_a_work_nobody_recorded_is_refused(self, recorded: ManuscriptStore) -> None:
        with pytest.raises(HTTPException) as raised:
            works.activity("nowhere")

        assert raised.value.status_code == 404


class TestHistoryCountsWhatFailed:
    """How far a run got is which files its room holds, so a run that produced
    nothing says so rather than being absent."""

    def test_every_run_is_listed(self, recorded: ManuscriptStore) -> None:
        ran(recorded, "s1")
        ran(recorded, "s2", produced=True)

        held = works.history("atlas")

        assert {one.session: one.produced for one in held} == {
            "s1": False,
            "s2": True,
        }

    def test_how_far_a_run_got_is_which_files_it_left(
        self, recorded: ManuscriptStore
    ) -> None:
        room = ran(recorded, "s1", produced=True)
        (room / ADOPTED_FILE).write_text("prose", encoding="utf-8")

        held = works.history("atlas")

        assert held[0].produced and held[0].adopted

    def test_a_run_that_produced_nothing_says_so_rather_than_being_absent(
        self, recorded: ManuscriptStore
    ) -> None:
        ran(recorded, "s1")

        held = works.history("atlas")

        assert len(held) == 1
        assert not held[0].produced and not held[0].adopted

    def test_naming_a_part_narrows_it(self, recorded: ManuscriptStore) -> None:
        ran(recorded, "s1")

        assert works.history("atlas", key="02/03/9.9.9") == []
        assert len(works.history("atlas", key="02/03/2.3.2")) == 1

    def test_a_work_that_has_never_run_has_no_history(
        self, recorded: ManuscriptStore
    ) -> None:
        assert works.history("atlas") == []
