"""The tree an author watches, and the one place a parked question is answered.

Every other route over a work reports what a command line already reports.
This one does something a terminal cannot: it takes an answer, and taking an
answer is what lets a part that parked run again. So what is worth pinning is
the state the routes leave behind rather than the shapes they return — that an
answered question stops being open, that its part becomes runnable, and that a
part still waiting on something else does not.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from inkwell.agent.glossary import DECLARED_VOCABULARY, write_chapter_glossary
from inkwell.environment.web.routes import works
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
