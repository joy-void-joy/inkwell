"""The tree an author watches, and the one place a parked question is answered.

Every other route over a work reports what a command line already reports.
This one does something a terminal cannot: it takes an answer, and taking an
answer is what lets a part that parked run again. So what is worth pinning is
the state the routes leave behind rather than the shapes they return — that an
answered question stops being open, that its part becomes runnable, and that a
part still waiting on something else does not.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import inkwell.manuscript.loop as loop_mod
from inkwell.agent.glossary import DECLARED_VOCABULARY, write_chapter_glossary
from inkwell.agent.google_auth import GoogleAuthError
from inkwell.environment.web import work_loops
from inkwell.environment.web.models import StageEvent
from inkwell.environment.web.routes import works
from inkwell.environment.web.session_manager import SessionManager
from inkwell.environment.web.work_loops import LoopAlreadyRunning, WorkLoopManager
from inkwell.manuscript.loop import (
    PassReport,
    WorkAlreadyRunning,
    WorkLease,
    run_loop,
)
from inkwell.manuscript.runner import PartOutcome
from inkwell.manuscript.graph import adopted, readings
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.mailbox import PartMailbox, PartQuestion, question_id
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.vocabulary import abbreviations_in, as_glossary

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.1 Bio Risk

Prose about engineered pathogens and biosecurity.

## 2.3.2 Cyber Risk

Prose about ASI and offensive capability in cyber operations.
"""

ABBREVIATIONS = "*[ASI]: Artificial Superintelligence.\n"

CYBER = "02/03/2.3.2"


@pytest.fixture(autouse=True)
def no_loops_left_running() -> Iterator[None]:
    """Leave the loop holder as each test found it.

    The holder is the app's, set once at startup, so a test that wires one in
    would otherwise decide what the next test sees — and the routes answer
    differently depending on whether a loop could be running.
    """
    yield
    works.holder.loops = None


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManuscriptStore:
    """A work imported, adopted, and reachable through the routes."""
    chapters = tmp_path / "chapters"
    chapter = chapters / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")

    store = ManuscriptStore(root=tmp_path / "manuscripts")
    tree = read_manuscript(chapters, title="A Work")
    store.publish_tree("work", tree)
    vocabulary = abbreviations_in(ABBREVIATIONS)
    path = store.glossary_path("work", DECLARED_VOCABULARY)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_chapter_glossary(path, as_glossary(vocabulary))
    store.publish_state(
        "work", adopted(store.load_state("work"), readings(tree, vocabulary))
    )

    monkeypatch.setattr(works, "manuscript_store", lambda: store)
    return store


class TestTheTreeShowsEveryPartAndItsState:
    def test_a_settled_work_reports_nothing_outstanding(
        self, recorded: ManuscriptStore
    ) -> None:
        tree = works.work_tree("work")

        assert tree.settled
        assert tree.outstanding == 0
        assert CYBER in [node.key for node in tree.nodes]

    def test_parts_above_the_leaves_are_shown_but_never_stale(
        self, recorded: ManuscriptStore
    ) -> None:
        """A chapter is never out of date; the parts under it are."""
        tree = works.work_tree("work")
        section = next(node for node in tree.nodes if node.key == "02/03")

        assert not section.leaf
        assert section.staleness == "fresh"

    def test_a_part_carries_the_part_above_it(self, recorded: ManuscriptStore) -> None:
        tree = works.work_tree("work")
        cyber = next(node for node in tree.nodes if node.key == CYBER)

        assert cyber.parent == "02/03"
        assert cyber.leaf

    def test_a_work_nobody_imported_is_a_clear_404(
        self, recorded: ManuscriptStore
    ) -> None:
        with pytest.raises(HTTPException) as raised:
            works.work_tree("nothing")

        assert raised.value.status_code == 404
        assert "import it first" in raised.value.detail


class TestAskingForAPartMakesItOutstanding:
    def test_a_requested_part_shows_why(self, recorded: ManuscriptStore) -> None:
        node = works.request_part(
            "work", CYBER, works.RequestRevision(reason="the July intrusion")
        )

        assert node.staleness == "requested"
        assert node.standing == "requested"
        assert "the July intrusion" in node.reasons
        assert not works.work_tree("work").settled

    def test_asking_for_a_part_that_is_not_there_is_a_404(
        self, recorded: ManuscriptStore
    ) -> None:
        with pytest.raises(HTTPException) as raised:
            works.request_part("work", "02/03/9.9.9", works.RequestRevision())

        assert raised.value.status_code == 404

    def test_a_part_a_run_is_holding_is_refused(
        self, recorded: ManuscriptStore
    ) -> None:
        """The standing this would write over is the lease, and the run named
        in it is the only way back to what is happening to the part."""
        recorded.publish_state(
            "work",
            recorded.load_state("work").declared(CYBER, "running", "picked up", "run1"),
        )

        with pytest.raises(HTTPException) as raised:
            works.request_part("work", CYBER, works.RequestRevision(reason="sharpen"))

        assert raised.value.status_code == 409
        assert "run1" in raised.value.detail
        assert works.one_part(recorded, "work", CYBER).session == "run1"


class TestAnsweringIsWhatLetsAParkedPartRunAgain:
    def parked(self, store: ManuscriptStore, *prompts: str) -> PartMailbox:
        """A part parked on however many questions were given."""
        mailbox = PartMailbox(root=store.work_dir("work"))
        for prompt in prompts:
            mailbox.ask(
                PartQuestion(
                    id=question_id(CYBER, prompt),
                    work="work",
                    asker=CYBER,
                    addressed_to="02/03",
                    prompt=prompt,
                )
            )
        store.publish_state(
            "work", store.load_state("work").declared(CYBER, "parked", prompts[0])
        )
        return mailbox

    def test_an_open_question_shows_which_part_waits_on_it(
        self, recorded: ManuscriptStore
    ) -> None:
        self.parked(recorded, "Update the 2024 figure?")
        waiting = works.open_questions("work")

        assert [held.asker for held in waiting] == [CYBER]
        assert waiting[0].addressed_to == "02/03"

    def test_answering_the_last_question_makes_the_part_runnable(
        self, recorded: ManuscriptStore
    ) -> None:
        mailbox = self.parked(recorded, "Update the 2024 figure?")
        identifier = mailbox.open()[0].id

        works.answer_question("work", identifier, works.AnswerRequest(value="yes"))

        assert works.open_questions("work") == []
        assert recorded.load_state("work").standing(CYBER) == "requested"

    def test_a_part_with_another_question_open_stays_parked(
        self, recorded: ManuscriptStore
    ) -> None:
        """Otherwise it runs, parks on the second, and pays a run to re-ask."""
        mailbox = self.parked(recorded, "Which figure?", "And which framing?")
        first = mailbox.open()[0].id

        works.answer_question("work", first, works.AnswerRequest(value="the 2026 one"))

        assert len(works.open_questions("work")) == 1
        assert recorded.load_state("work").standing(CYBER) == "parked"

    def test_answering_twice_is_refused_rather_than_overwriting(
        self, recorded: ManuscriptStore
    ) -> None:
        mailbox = self.parked(recorded, "Which figure?")
        identifier = mailbox.open()[0].id
        works.answer_question("work", identifier, works.AnswerRequest(value="2026"))

        with pytest.raises(HTTPException) as raised:
            works.answer_question("work", identifier, works.AnswerRequest(value="2024"))

        assert raised.value.status_code == 404


class TestAskingWhatAChangeWouldReach:
    def test_a_declared_term_names_the_parts_that_use_it(
        self, recorded: ManuscriptStore
    ) -> None:
        reached = works.reaches("work", subject="ASI", kind="term")

        assert reached.parts == (CYBER,)

    def test_a_term_nothing_uses_reaches_nothing(
        self, recorded: ManuscriptStore
    ) -> None:
        assert works.reaches("work", subject="Nothing", kind="term").parts == ()

    def test_a_kind_that_is_not_one_is_refused(self, recorded: ManuscriptStore) -> None:
        with pytest.raises(HTTPException) as raised:
            works.reaches("work", subject="ASI", kind="vibes")

        assert raised.value.status_code == 400


class TestRecordingAWorkFromTheBrowser:
    def test_an_import_records_the_work_and_adopts_it(
        self, recorded: ManuscriptStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adopting is what makes the first useful pass small rather than total."""
        monkeypatch.setattr(works, "manuscript_store", lambda: recorded)

        result = works.record_work(
            works.ImportRequest(
                work="second",
                chapters=str(tmp_path / "chapters"),
                title="Another Work",
            )
        )

        assert result.parts == 2
        assert result.adopted == 2
        assert works.work_tree("second").settled

    def test_declining_to_adopt_leaves_every_part_unbuilt(
        self, recorded: ManuscriptStore, tmp_path: Path
    ) -> None:
        works.record_work(
            works.ImportRequest(
                work="unbuilt", chapters=str(tmp_path / "chapters"), adopt=False
            )
        )
        tree = works.work_tree("unbuilt")

        assert tree.outstanding == 2
        assert all(node.staleness == "never-built" for node in tree.nodes if node.leaf)

    def test_a_directory_that_is_not_there_is_refused(
        self, recorded: ManuscriptStore, tmp_path: Path
    ) -> None:
        with pytest.raises(HTTPException) as raised:
            works.record_work(
                works.ImportRequest(work="nowhere", chapters=str(tmp_path / "absent"))
            )

        assert raised.value.status_code == 422

    def test_a_work_id_that_could_leave_the_store_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The id names a directory, so the type refuses what a path would not."""
        with pytest.raises(ValidationError):
            works.ImportRequest(work="../escaped", chapters=str(tmp_path))


class TestSayingWhatARunWouldCostBeforeItRuns:
    def test_a_settled_work_would_run_nothing(self, recorded: ManuscriptStore) -> None:
        assert works.preview_run("work").parts == ()

    def test_a_requested_part_is_what_would_run(
        self, recorded: ManuscriptStore
    ) -> None:
        works.request_part("work", CYBER, works.RequestRevision(reason="why"))

        would = works.preview_run("work")

        assert [verdict.key for verdict in would.parts] == [CYBER]
        assert would.outstanding == 1

    def test_a_limit_cuts_the_preview_the_way_it_cuts_the_pass(
        self, recorded: ManuscriptStore
    ) -> None:
        works.request_part("work", CYBER, works.RequestRevision())
        works.request_part("work", "02/03/2.3.1", works.RequestRevision())

        would = works.preview_run("work", limit=1)

        assert len(would.parts) == 1
        assert would.outstanding == 2

    def test_naming_a_part_previews_that_part_rather_than_the_first_one(
        self, recorded: ManuscriptStore
    ) -> None:
        """What a limit cannot express. A limit takes the first parts in tree
        order, so a row offering to run itself has to be able to name itself."""
        works.request_part("work", CYBER, works.RequestRevision())
        works.request_part("work", "02/03/2.3.1", works.RequestRevision())

        would = works.preview_run("work", part=[CYBER])

        assert [verdict.key for verdict in would.parts] == [CYBER]

    def test_a_named_part_that_would_not_run_says_why(
        self, recorded: ManuscriptStore
    ) -> None:
        """The browser shows this instead of starting a pass that runs nothing
        and reads back as a finished book."""
        would = works.preview_run("work", part=[CYBER])

        assert would.parts == ()
        assert [held.key for held in would.passed_over] == [CYBER]
        assert would.passed_over[0].reason == "up to date"


class TestALoopIsRefusedWhereItCouldNotHelp:
    async def test_a_work_whose_checkout_is_gone_is_refused(
        self, recorded: ManuscriptStore, tmp_path: Path
    ) -> None:
        """The answer is about a directory, not about two hundred rewrites."""
        works.set_loops(WorkLoopManager())
        tree = recorded.load_tree("work")
        assert tree is not None
        recorded.publish_tree(
            "work", tree.model_copy(update={"root": str(tmp_path / "moved")})
        )

        with pytest.raises(HTTPException) as raised:
            await works.start_run("work", works.RunRequest())

        assert raised.value.status_code == 409
        assert "which is not there" in raised.value.detail

    async def test_a_second_loop_over_one_work_is_refused(
        self, recorded: ManuscriptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loop is the single writer of state; two would lease the same parts."""
        loops = WorkLoopManager()
        works.set_loops(loops)
        monkeypatch.setattr(loops, "running", lambda work: True)

        with pytest.raises(HTTPException) as raised:
            await works.start_run("work", works.RunRequest())

        assert raised.value.status_code == 409

    def test_a_work_with_no_loop_reports_one_that_is_not_running(
        self, recorded: ManuscriptStore
    ) -> None:
        works.set_loops(WorkLoopManager())

        assert not works.run_status("work").running

    def test_a_loop_owned_by_another_process_still_reports_as_running(
        self, recorded: ManuscriptStore
    ) -> None:
        works.set_loops(WorkLoopManager())

        with WorkLease.acquire(recorded, "work"):
            assert works.run_status("work").running

    async def test_the_shared_loop_entrypoint_cannot_bypass_the_work_lease(
        self, recorded: ManuscriptStore
    ) -> None:
        tree = recorded.load_tree("work")
        assert tree is not None

        with WorkLease.acquire(recorded, "work"):
            with pytest.raises(WorkAlreadyRunning):
                await run_loop(recorded, "work", tree)

    async def test_a_loop_that_could_not_write_a_part_buys_no_plan(
        self, recorded: ManuscriptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every part is written into a document, so no document is every part."""
        tree = recorded.load_tree("work")
        assert tree is not None
        passes: list[str] = []
        monkeypatch.setattr(
            loop_mod, "document_fault", lambda: "Failed to refresh Google token"
        )
        monkeypatch.setattr(loop_mod, "run_pass", lambda *a, **k: passes.append("ran"))

        with pytest.raises(GoogleAuthError, match="Failed to refresh"):
            await run_loop(recorded, "work", tree)

        assert passes == []

    async def test_two_managers_cannot_run_the_same_work(
        self, recorded: ManuscriptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = WorkLoopManager()
        second = WorkLoopManager()
        tree = recorded.load_tree("work")
        assert tree is not None
        release = asyncio.Event()

        async def waiting(
            store: ManuscriptStore, work: str, manuscript: object, **held: object
        ) -> tuple[PassReport, ...]:
            await release.wait()
            return ()

        monkeypatch.setattr(work_loops, "run_loop", waiting)
        first.start(
            recorded,
            "work",
            tree,
            passes=1,
            limit=0,
            concurrency=1,
            reconciling=True,
        )

        with pytest.raises(LoopAlreadyRunning):
            second.start(
                recorded,
                "work",
                tree,
                passes=1,
                limit=0,
                concurrency=1,
                reconciling=True,
            )

        release.set()
        held = first.loop_for("work")
        assert held is not None
        await held.task


class TestAPartRunIsASessionLikeAnyOther:
    """It always was one — a trace, a stage, a document, a spend — but one the
    web layer had never been told about, so the list read it back off its own
    unfinished record and called it interrupted for as long as it ran."""

    async def test_an_opened_part_run_is_a_live_session(self) -> None:
        sessions = SessionManager()

        work_loops.WatchedByTheBrowser(sessions).opening(CYBER, "run1")

        held = sessions.handle_for("run1")
        assert held is not None
        assert held.status == "running"
        assert [held.session_id for held in sessions.list_sessions()[:1]] == ["run1"]

    async def test_the_run_is_handed_the_instruments_it_publishes_through(
        self,
    ) -> None:
        """Without these the pipeline runs blind and the handle stays empty,
        which is the whole difference between adopting one and listing it."""
        sessions = SessionManager()

        observers = work_loops.WatchedByTheBrowser(sessions).opening(CYBER, "run1")

        held = sessions.handle_for("run1")
        assert held is not None
        assert observers.listener is held.listener
        assert observers.state is held.state
        assert observers.cost is held.cost

    async def test_a_part_run_can_be_stopped_on_its_own(self) -> None:
        """Without this the only way to be rid of one stuck part run was to
        take the whole loop down, or wait it out."""
        sessions = SessionManager()
        watch = work_loops.WatchedByTheBrowser(sessions)
        stopped: list[bool] = []

        async def running() -> None:
            watch.opening(CYBER, "run1")
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.append(True)
                raise

        turn = asyncio.create_task(running())
        await asyncio.sleep(0)
        assert await sessions.send_action("run1", "quit")
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert stopped == [True]

    async def test_a_finished_part_run_stops_reading_as_running(self) -> None:
        sessions = SessionManager()
        watch = work_loops.WatchedByTheBrowser(sessions)
        watch.opening(CYBER, "run1")

        watch.closed(CYBER, "run1", PartOutcome(key=CYBER, text="prose"))

        held = sessions.handle_for("run1")
        assert held is not None
        assert held.status == "completed"

    def test_a_parked_part_run_reads_as_paused_rather_than_broken(self) -> None:
        """It asked the author something and is waiting, which is the standing
        the surface already knows how to offer a way back into."""
        parked = PartOutcome(key=CYBER, text="prose", questions=("which figure?",))

        assert work_loops.ended_as(parked) == "paused"

    def test_a_failed_part_run_says_so(self) -> None:
        broken = PartOutcome(key=CYBER, failure="no prose")

        assert work_loops.ended_as(broken) == "failed"


class TestWhatIsBeingWrittenRightNow:
    """The question a pass report cannot answer, because it arrives too late.

    A pass over a book is long, and until it ends the only public account of
    it was a spinner: an author who started one could not tell which
    subsection was being written, by which run, or whether anything at all was
    happening. These read the lease, which is recorded per part as the pass
    takes it and therefore says so while it is still true.
    """

    def held(self, store: ManuscriptStore, holder: str = "run1") -> None:
        """One part leased by a run, the way a pass leases it."""
        store.publish_state(
            "work",
            store.load_state("work").declared(CYBER, "running", "picked up", holder),
        )

    def test_a_leased_part_is_reported_as_in_flight(
        self, recorded: ManuscriptStore
    ) -> None:
        self.held(recorded)

        flying = works.in_flight(recorded, "work")

        assert [held.key for held in flying] == [CYBER]
        assert flying[0].session == "run1"
        assert flying[0].title == "2.3.2 Cyber Risk"
        assert flying[0].reason == "picked up"

    def test_the_tree_carries_it_so_one_fetch_answers_both(
        self, recorded: ManuscriptStore
    ) -> None:
        """Carried on the tree rather than beside it: a page that fetched them
        separately could render a settled work over a run in progress."""
        works.set_loops(WorkLoopManager())
        self.held(recorded)

        assert [held.key for held in works.work_tree("work").in_flight] == [CYBER]

    def test_a_part_held_by_a_run_that_is_gone_reads_as_orphaned(
        self, recorded: ManuscriptStore
    ) -> None:
        """The case worth surfacing rather than hiding: it looks exactly like
        work in progress and will never finish on its own."""
        works.set_loops(WorkLoopManager(SessionManager()))
        self.held(recorded)

        assert works.in_flight(recorded, "work")[0].status == "orphaned"

    def test_a_part_waiting_its_turn_is_queued_and_not_orphaned(
        self, recorded: ManuscriptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pass leases every part it means to run before running any of them,
        so a part behind the concurrency cap is leased with no run yet. Calling
        that orphaned sends somebody to clear a lease about to be used."""
        loops = WorkLoopManager(SessionManager())
        works.set_loops(loops)
        monkeypatch.setattr(loops, "running", lambda work: True)
        self.held(recorded)

        assert works.in_flight(recorded, "work")[0].status == "queued"

    def test_a_live_run_reports_its_own_stage_and_spend(
        self, recorded: ManuscriptStore
    ) -> None:
        """What adoption bought: the run answers for itself instead of being
        read back off an unfinished record and called interrupted."""
        sessions = SessionManager()
        works.set_loops(WorkLoopManager(sessions))
        self.held(recorded, "run2")
        sessions.adopt("run2").state.stage = "research"

        flying = works.in_flight(recorded, "work")

        assert flying[0].status == "running"
        assert flying[0].stage == "research"

    def test_the_announced_preflight_stage_reaches_the_work_page(
        self, recorded: ManuscriptStore
    ) -> None:
        sessions = SessionManager()
        works.set_loops(WorkLoopManager(sessions))
        self.held(recorded, "run3")
        sessions.adopt("run3").events = [StageEvent(stage="brief", description="Brief")]

        assert works.in_flight(recorded, "work")[0].stage == "brief"

    def test_a_work_at_rest_has_nothing_in_flight(
        self, recorded: ManuscriptStore
    ) -> None:
        assert works.in_flight(recorded, "work") == ()


class TestAPostedRunActuallySchedulesThePass:
    """Routed to rather than called, because the dispatch is the thing at issue.

    A loop is a task, and a task needs a running event loop to belong to. An
    endpoint declared synchronous is handed to a worker thread, which has none,
    so every start from the browser raised where the pass should have been
    scheduled — while calling ``start_run`` directly, as the refusal tests
    above do, went nowhere near that and passed throughout.
    """

    async def test_starting_a_loop_over_http_schedules_it(
        self, recorded: ManuscriptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        works.set_loops(WorkLoopManager())
        asked: list[str] = []

        async def scheduled(
            store: ManuscriptStore, work: str, manuscript: object, **held: object
        ) -> tuple[PassReport, ...]:
            asked.append(work)
            return ()

        monkeypatch.setattr(work_loops, "run_loop", scheduled)
        routed = FastAPI()
        routed.include_router(works.router)

        async with AsyncClient(
            transport=ASGITransport(app=routed), base_url="http://work"
        ) as browser:
            answered = await browser.post("/api/works/work/run", json={})

        assert answered.status_code == 202
        assert answered.json()["running"]
        held = works.get_loops().loop_for("work")
        assert held is not None
        assert await held.task == ()
        assert asked == ["work"]

    async def test_selecting_a_fresh_part_and_running_it_is_one_request(
        self, recorded: ManuscriptStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        works.set_loops(WorkLoopManager())
        asked: list[tuple[str, ...]] = []

        async def scheduled(
            store: ManuscriptStore, work: str, manuscript: object, **held: object
        ) -> tuple[PassReport, ...]:
            asked.append(cast(tuple[str, ...], held["only"]))
            return ()

        monkeypatch.setattr(work_loops, "run_loop", scheduled)
        routed = FastAPI()
        routed.include_router(works.router)

        async with AsyncClient(
            transport=ASGITransport(app=routed), base_url="http://work"
        ) as browser:
            answered = await browser.post(f"/api/works/work/parts/{CYBER}/run", json={})

        assert answered.status_code == 202
        held = works.get_loops().loop_for("work")
        assert held is not None
        assert await held.task == ()
        assert asked == [(CYBER,)]
        assert recorded.load_state("work").standing(CYBER) == "requested"

    async def test_losing_the_work_lease_leaves_no_part_request_behind(
        self, recorded: ManuscriptStore
    ) -> None:
        works.set_loops(WorkLoopManager())
        routed = FastAPI()
        routed.include_router(works.router)

        with WorkLease.acquire(recorded, "work"):
            async with AsyncClient(
                transport=ASGITransport(app=routed), base_url="http://work"
            ) as browser:
                answered = await browser.post(
                    f"/api/works/work/parts/{CYBER}/run", json={}
                )

        assert answered.status_code == 409
        assert recorded.load_state("work").standing(CYBER) == "idle"


class TestClearingIsTheWayOutOfAStickyStanding:
    def test_a_requested_part_can_be_taken_back(
        self, recorded: ManuscriptStore
    ) -> None:
        works.set_loops(WorkLoopManager())
        works.request_part("work", CYBER, works.RequestRevision(reason="changed mind"))

        node = works.clear_part("work", CYBER)

        assert node.standing == "idle"
        assert node.staleness == "fresh"
        assert works.work_tree("work").settled

    def test_a_failed_part_can_be_tried_again(self, recorded: ManuscriptStore) -> None:
        """A failed part stays failed on its own, which is why this has to exist."""
        works.set_loops(WorkLoopManager())
        recorded.publish_state(
            "work", recorded.load_state("work").declared(CYBER, "failed", "it broke")
        )

        assert works.clear_part("work", CYBER).standing == "idle"

    def test_clearing_is_refused_while_a_loop_holds_the_work(
        self, recorded: ManuscriptStore
    ) -> None:
        with WorkLease.acquire(recorded, "work"):
            with pytest.raises(HTTPException) as raised:
                works.clear_part("work", CYBER)

        assert raised.value.status_code == 409


class TestTheTreeCarriesWhatARowNeedsToBeReadable:
    def test_a_chapter_rolls_up_what_is_outstanding_beneath_it(
        self, recorded: ManuscriptStore
    ) -> None:
        """The question an author opens a two-hundred-part tree with."""
        works.request_part("work", CYBER, works.RequestRevision())
        tree = works.work_tree("work")

        chapter = next(node for node in tree.nodes if node.key == "02")

        assert chapter.below == 2
        assert chapter.outstanding_below == 1

    def test_a_part_names_the_run_that_last_held_it(
        self, recorded: ManuscriptStore
    ) -> None:
        recorded.publish_state(
            "work",
            recorded.load_state("work").declared(CYBER, "failed", "broke", "abc123"),
        )
        tree = works.work_tree("work")

        assert (
            next(node for node in tree.nodes if node.key == CYBER).session == "abc123"
        )

    def test_a_work_read_from_a_missing_checkout_says_so_once(
        self, recorded: ManuscriptStore, tmp_path: Path
    ) -> None:
        held = recorded.load_tree("work")
        assert held is not None
        recorded.publish_tree(
            "work", held.model_copy(update={"root": str(tmp_path / "moved")})
        )

        tree = works.work_tree("work")

        assert not tree.rooted
        assert tree.blocked == 2
        assert not tree.settled
