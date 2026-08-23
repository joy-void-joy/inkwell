"""Reading a work of many parts into the tree a run works against.

What is worth pinning is what a filesystem walk would get wrong and the
declared order gets right, that the tree goes one level past the files because
that is where the revisable unit lives, and that a key survives the kinds of
edit an author actually makes — since state is keyed on it and a key that
moved is state that was lost.
"""

import asyncio
from pathlib import Path

import pytest
from lup.runtime.models import TurnTextBlock

import inkwell.agent.client as client
from inkwell.agent.glossary import (
    DECLARED_VOCABULARY,
    ChapterGlossary,
    GlossaryEntry,
    NodeGlossary,
    read_glossary,
    write_chapter_glossary,
)
from inkwell.agent.models import (
    AgentSessionResult,
    ArticlePlan,
    WritingOutput,
)
from inkwell.agent.pipeline import PipelineListener
from inkwell.agent.stages import unknown_format
from inkwell.manuscript.facts import (
    Consumption,
    Dependency,
    ProposedChange,
    consumption_of,
)
from inkwell.manuscript.findings import WorkFindings
from inkwell.manuscript.graph import (
    PartReading,
    WorkSweep,
    adopted,
    readings,
    sweep,
)
from inkwell.manuscript.budget import LengthBudget, budget_for
from inkwell.manuscript.inheritance import (
    ADOPTED_FILE,
    Audit,
    Dropped,
    InheritanceReader,
)
from inkwell.manuscript.inventory import inventory_of
from inkwell.manuscript.loop import (
    Recorded,
    narrowed,
    recording,
    run_pass,
    schedulable,
    unpicked,
)
from inkwell.manuscript.mailbox import (
    PartAnswer,
    PartMailbox,
    PartQuestion,
    escalated_to,
    question_id,
)
from inkwell.manuscript.ingest import (
    heading_titles,
    nav_entries,
    ordinal_of,
    read_manuscript,
)
from inkwell.manuscript import runner as runner_module
from inkwell.manuscript.brief import (
    BriefWriter,
    ComposedBrief,
    CorpusPusher,
    EditorialSection,
    PushedCorpusClaim,
    evidence_for,
)
from inkwell.manuscript.planner import PartPlan, WorkBriefs
from inkwell.manuscript.runner import (
    HANDOFF_FILE,
    PRODUCED_FILE,
    PartOutcome,
    PartRunAgents,
    PartRunObservers,
    PartRunWatch,
    PartHandoff,
    PartPublisher,
    around,
    coinages,
    ledger,
    named_in,
    part_instruction,
    run_part,
)
from inkwell.manuscript.splice import (
    HeadingLost,
    PartNotFound,
    held_text,
    opens_with,
    spliced,
)
from inkwell.manuscript.state import WorkState, digest_of
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.manuscript.vocabulary import (
    abbreviations_in,
    read_abbreviations,
    terms_used,
)

CHAPTER_PAGES = """\
nav:
- Chapter 02: README.md
- 2.1 - Risk Decomposition: 01.md
- 2.3 - Misuse Risks: 03.md
- Appendix:
  - 2.A1 - X-Risk Scenarios: A1.md
title: 02 - Risks
"""

CHAPTER_META = """\
chapter_number: 2
chapter_title: Risks
authors:
- Markov Grey
"""

MISUSE = """\
# 2.3 Misuse Risks

Some opening prose.

## 2.3.1 Bio Risk {: #01}

Prose.

## 2.3.2 Cyber Risk {: #02}

Prose about cyber risk.

```python
## not a heading, it is inside a fence
```

## 2.3.3 Autonomous Weapons Risk {: #03}
"""


FIGURE_BLOCK = """
<figure markdown="span">
![alt](Images/1Wx_Image_14.png){ loading=lazy }
  <figcaption markdown="1"><b>Figure 2.14:</b> Hacking websites with agents.</figcaption>
</figure>
"""


class PassesThrough(InheritanceReader):
    """An inheritance pass that adopts the fresh draft exactly as it stands.

    Adopting verbatim keeps these tests about what they were about: whatever
    the run produced is what reaches the splice, as it did before there was a
    pass in between.
    """

    def __init__(self, dropped: tuple[Dropped, ...] = ()) -> None:
        self.dropped = dropped

    async def read(self, task: str, room: Path) -> Audit:
        produced = room / PRODUCED_FILE
        (room / ADOPTED_FILE).write_text(
            produced.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return Audit(dropped=list(self.dropped))


class PlansPart(BriefWriter):
    """A prose-blind planner stub, so no unit test reaches a model."""

    async def compose(self, task: str, root: Path) -> ComposedBrief:
        return ComposedBrief(
            thesis="The part's successor.",
            opening=EditorialSection(
                title="The successor",
                establishes="The planned point.",
                order_reason="It opens the part.",
                evidence=["The test fixture."],
                handoff="The part can conclude.",
                key_points=[],
            ),
        )


class PushesNothing(CorpusPusher):
    """An empty corpus, so unit part runs do no external retrieval."""

    async def push(self, topic: str) -> tuple[PushedCorpusClaim, ...]:
        return ()


OFFLINE = PartRunAgents(
    briefing=PlansPart(), corpus=PushesNothing(), inheriting=PassesThrough()
)
"""The whole set of readers a part run buys, stubbed.

Handed over by every test here that runs a part, so a unit test never reaches a
model. The set rather than a member apiece is the point: stubbing two of three
seams leaves the third live, and a test that reaches a model finds out by taking
four minutes instead of a second.
"""


def atlas_like(root: Path) -> Path:
    """A directory shaped the way the Atlas is, small enough to read at once."""
    chapters = root / "chapters"
    chapter = chapters / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / ".meta.yml").write_text(CHAPTER_META, encoding="utf-8")
    (chapter / "README.md").write_text("# Chapter 02\n", encoding="utf-8")
    (chapter / "01.md").write_text("# 2.1 Risk Decomposition\n", encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    (chapter / "A1.md").write_text("# 2.A1 X-Risk Scenarios\n", encoding="utf-8")
    return chapters


class TestTheDeclaredOrderIsRead:
    """The nav file is read rather than the directory walked, because sorting
    filenames puts A1.md before 01.md and the appendix belongs at the end."""

    def test_sections_come_in_nav_order(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        chapter = work.children[0]

        assert [child.key for child in chapter.children] == [
            "02/README",
            "02/01",
            "02/03",
            "02/appendix",
        ]

    def test_the_appendix_stays_a_group(self, tmp_path: Path) -> None:
        """It holds parts without being one, so nothing runs against it."""
        work = read_manuscript(atlas_like(tmp_path))
        appendix = work.children[0].children[-1]

        assert appendix.kind == "group"
        assert [child.key for child in appendix.children] == ["02/appendix/A1"]

    def test_the_chapter_is_named_by_its_own_metadata(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert work.children[0].title == "Risks"

    def test_a_nav_wildcard_declares_no_part(self) -> None:
        """'...' means 'everything else', which the directory answers."""
        assert nav_entries(["index.md", "..."]) == nav_entries(["index.md"])


class TestTheTreeGoesPastTheFiles:
    """The revisable unit is a heading inside a section file, not the file."""

    def test_subsections_become_parts_of_their_own(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        misuse = work.node("02/03")

        assert misuse is not None
        assert [child.key for child in misuse.children] == [
            "02/03/2.3.1",
            "02/03/2.3.2",
            "02/03/2.3.3",
        ]

    def test_a_subsection_points_at_the_file_holding_it(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        cyber = work.node("02/03/2.3.2")

        assert cyber is not None
        assert cyber.title == "2.3.2 Cyber Risk"
        assert cyber.path == "02/03.md"

    def test_the_anchor_is_not_part_of_the_title(self, tmp_path: Path) -> None:
        """`{: #02}` is presentation, and would otherwise be read as the name."""
        work = read_manuscript(atlas_like(tmp_path))
        cyber = work.node("02/03/2.3.2")

        assert cyber is not None
        assert "{:" not in cyber.title

    def test_a_heading_inside_a_fence_is_not_a_part(self) -> None:
        """Read off parsed tokens, so a `##` in code is code."""
        assert "not a heading, it is inside a fence" not in heading_titles(MISUSE)

    def test_a_file_with_no_subsections_is_itself_the_leaf(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        decomposition = work.node("02/01")

        assert decomposition is not None
        assert decomposition.children == []
        assert decomposition in list(work.leaves())


class TestKeysSurviveAnEdit:
    """State is keyed on these, so a key that moves is state that was lost."""

    def test_a_subsection_keys_on_its_own_numbering(self) -> None:
        """Not on position — inserting 2.3.0 must not renumber the rest."""
        assert ordinal_of("2.3.2 Cyber Risk") == "2.3.2"

    def test_a_heading_with_no_numbering_keys_on_its_words(self) -> None:
        assert ordinal_of("Cyber Risk") == ""

    def test_a_numbered_list_item_and_a_heading_key_alike(self) -> None:
        assert ordinal_of("3. Something") == ordinal_of("3 Something")


class TestStateIsKeptBesideTheTree:
    """Re-importing a work reads the source again; it must not have an opinion
    about what anybody asked of the parts."""

    def test_an_untouched_part_reads_idle(self) -> None:
        assert WorkState().standing("02/03/2.3.2") == "idle"

    def test_declaring_a_standing_replaces_its_entry(self) -> None:
        state = WorkState().declared(
            "02/03/2.3.2", "requested", "the Hugging Face story"
        )
        moved = state.declared("02/03/2.3.2", "running", "picked up")

        assert moved.standing("02/03/2.3.2") == "running"
        assert len(moved.records) == 1

    def test_active_is_what_a_pass_has_left(self) -> None:
        state = WorkState().declared("02/03/2.3.2", "requested", "stale")

        assert [entry.key for entry in state.active()] == ["02/03/2.3.2"]
        assert state.declared("02/03/2.3.2", "idle", "done").active() == ()


class TestDirtinessIsDerivedRatherThanStored:
    """The one thing a build system must never persist. A stored flag is a
    prediction that goes stale; every question here is asked afresh."""

    def test_a_part_nothing_built_is_never_built(self) -> None:
        assert WorkState().verdict("02/03/2.3.2", "prose").staleness == "never-built"

    def test_a_part_built_from_what_it_holds_is_fresh(self) -> None:
        state = WorkState().built("02/03/2.3.2", source="prose", consumed=Consumption())

        assert state.verdict("02/03/2.3.2", "prose").staleness == "fresh"
        assert not state.verdict("02/03/2.3.2", "prose").dirty()

    def test_editing_the_text_underneath_shows_up_without_being_told(self) -> None:
        """Nobody records that an author edited a file; the digest notices."""
        state = WorkState().built("02/03/2.3.2", source="prose", consumed=Consumption())

        assert state.verdict("02/03/2.3.2", "different prose").staleness == (
            "source-moved"
        )

    def test_asking_for_a_part_is_enough_on_its_own(self) -> None:
        state = WorkState().built("02/03/2.3.2", source="prose", consumed=Consumption())
        asked = state.declared("02/03/2.3.2", "requested", "currency")

        verdict = asked.verdict("02/03/2.3.2", "prose")
        assert verdict.staleness == "requested"
        assert "currency" in verdict.reasons


ESCAPE = Dependency(kind="term", subject="sandbox escape")
MESA = Dependency(kind="term", subject="mesa-optimization")


class TestPropagationKeysOnWhatChangedNotOnWhoChanged:
    """The whole reason a pass over a book with cycles in it settles."""

    def consuming(self, *depended: Dependency) -> WorkState:
        """A state where 2.3.2 was built having read ``depended``."""
        return WorkState().built(
            "02/03/2.3.2", source="prose", consumed=consumption_of(depended)
        )

    def test_a_change_reaches_what_consumed_it(self) -> None:
        state = self.consuming(MESA).changed(
            "04/01/4.1", [ProposedChange(dependency=MESA, detail="narrowed")]
        )

        verdict = state.verdict("02/03/2.3.2", "prose")
        assert verdict.staleness == "upstream"
        assert "narrowed" in verdict.reasons[0]

    def test_a_change_nobody_consumed_reaches_nothing(self) -> None:
        """A rewrite that redefines nothing shared costs the book nothing."""
        state = self.consuming(MESA).changed(
            "04/01/4.1", [ProposedChange(dependency=ESCAPE)]
        )

        assert state.verdict("02/03/2.3.2", "prose").staleness == "fresh"

    def test_a_part_is_not_dirtied_by_its_own_change(self) -> None:
        """Otherwise every run would dirty itself and no pass would ever end."""
        state = self.consuming(MESA).changed(
            "02/03/2.3.2", [ProposedChange(dependency=MESA)]
        )

        assert state.verdict("02/03/2.3.2", "prose").staleness == "fresh"

    def test_rebuilding_settles_the_part_without_retiring_the_fact(self) -> None:
        """The ledger is append-only: what reached one part must still reach
        every other part that has not caught up with it."""
        reached = self.consuming(MESA).built(
            "01/02/1.2.1", source="other", consumed=consumption_of([MESA])
        )
        state = reached.changed(
            "04/01/4.1", [ProposedChange(dependency=MESA, detail="narrowed")]
        )
        settled = state.built(
            "02/03/2.3.2", source="prose", consumed=consumption_of([MESA])
        )

        assert settled.verdict("02/03/2.3.2", "prose").staleness == "fresh"
        assert settled.verdict("01/02/1.2.1", "other").staleness == "upstream"
        assert len(settled.ledger.facts) == 1

    def test_a_cycle_between_two_parts_comes_to_rest(self) -> None:
        """Each dirties the other once, and neither has anything left to say."""
        state = WorkState()
        for key, reads in (("a", ESCAPE), ("b", MESA)):
            state = state.built(key, source=key, consumed=consumption_of([reads]))
        state = state.changed("b", [ProposedChange(dependency=ESCAPE)])
        state = state.changed("a", [ProposedChange(dependency=MESA)])

        assert state.verdict("a", "a").staleness == "upstream"
        assert state.verdict("b", "b").staleness == "upstream"

        for key in ("a", "b"):
            reads = ESCAPE if key == "a" else MESA
            state = state.built(key, source=key, consumed=consumption_of([reads]))

        assert state.verdict("a", "a").staleness == "fresh"
        assert state.verdict("b", "b").staleness == "fresh"


class TestWalkingTheWork:
    def test_leaves_are_what_a_run_can_be_about(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert "02/03/2.3.2" in [leaf.key for leaf in work.leaves()]
        assert "02/03" not in [leaf.key for leaf in work.leaves()]

    def test_an_unknown_key_finds_nothing(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert work.node("02/03/9.9.9") is None

    def test_a_node_walks_itself_and_its_parts(self) -> None:
        node = ManuscriptNode(
            key="a",
            kind="section",
            title="A",
            children=[ManuscriptNode(key="a/b", kind="subsection", title="B")],
        )

        assert [found.key for found in node.walk()] == ["a", "a/b"]

    def test_an_empty_work_walks_to_nothing(self) -> None:
        assert list(Manuscript(title="t", root="/tmp").walk()) == []


APPENDIX = """\
# 1.A1 Expert Opinions

## 1.A1.1 Surveys

Prose.

## 1.A1.2 Quotes

Prose.

## 1.A1.3 Prediction Markets

Prose.
"""

UNNUMBERED = """\
# A Section

## Overview

First.

## Overview

Second, and identically titled.
"""


class TestNoPartIsLostToAKeyAnotherPartTook:
    """The key is the identity every stamp and standing is filed under, so two
    parts sharing one is not a confusing tree — it is a part that vanished."""

    def test_an_appendix_numbers_with_letters_and_still_keys_apart(
        self, tmp_path: Path
    ) -> None:
        """Digits alone stop at the "A" and hand all three the same key."""
        chapters = tmp_path / "chapters"
        chapter = chapters / "01"
        chapter.mkdir(parents=True)
        (chapter / ".pages.yml").write_text(
            "nav:\n- 1.A1 - Expert Opinions: A1.md\n", encoding="utf-8"
        )
        (chapter / "A1.md").write_text(APPENDIX, encoding="utf-8")

        work = read_manuscript(chapters)
        assert [leaf.key for leaf in work.leaves()] == [
            "01/A1/1.A1.1",
            "01/A1/1.A1.2",
            "01/A1/1.A1.3",
        ]

    def test_an_appendix_ordinal_keeps_its_letters(self) -> None:
        assert ordinal_of("1.A1.2 Quotes") == "1.A1.2"

    def test_words_that_are_not_numbering_are_still_not_an_ordinal(self) -> None:
        """Alphanumeric levels must not turn every prose heading into one."""
        assert ordinal_of("Overview of the Field") == ""

    def test_two_headings_wanting_one_key_both_survive(self, tmp_path: Path) -> None:
        chapters = tmp_path / "chapters"
        chapter = chapters / "01"
        chapter.mkdir(parents=True)
        (chapter / ".pages.yml").write_text(
            "nav:\n- A Section: 01.md\n", encoding="utf-8"
        )
        (chapter / "01.md").write_text(UNNUMBERED, encoding="utf-8")

        keys = [leaf.key for leaf in read_manuscript(chapters).leaves()]
        assert len(keys) == 2
        assert len(set(keys)) == 2


class TestOneRewriteLeavesItsSiblingsAlone:
    """A splice rather than a reassembly, so an untouched subsection is
    byte-identical and never drifts through a model that re-emitted it."""

    def test_a_part_holds_its_own_span_of_a_shared_file(self) -> None:
        node = ManuscriptNode(
            key="02/03/2.3.2",
            kind="subsection",
            title="2.3.2 Cyber Risk",
            path="02/03.md",
            heading="2.3.2 Cyber Risk {: #02}",
        )
        held = held_text(MISUSE, node)

        assert held.startswith("## 2.3.2 Cyber Risk")
        assert "Prose about cyber risk." in held
        assert "Bio Risk" not in held
        assert "Autonomous Weapons" not in held

    def test_a_part_that_owns_its_file_holds_all_of_it(self) -> None:
        node = ManuscriptNode(key="02/01", kind="section", title="2.1", path="02/01.md")

        assert held_text("# 2.1 Risk Decomposition\n", node) == (
            "# 2.1 Risk Decomposition\n"
        )

    def test_replacing_one_part_leaves_the_others_byte_identical(self) -> None:
        rewritten = spliced(MISUSE, "2.3.2 Cyber Risk", "## 2.3.2 Cyber Risk\n\nNew.")

        assert "## 2.3.1 Bio Risk {: #01}\n\nProse.\n" in rewritten
        assert "## 2.3.3 Autonomous Weapons Risk {: #03}" in rewritten
        assert "Prose about cyber risk." not in rewritten
        assert "New." in rewritten

    def test_a_fenced_heading_is_not_a_boundary_to_splice_at(self) -> None:
        """The fence sits inside 2.3.2, so replacing it must take the fence."""
        rewritten = spliced(MISUSE, "2.3.2 Cyber Risk", "## 2.3.2 Cyber Risk\n\nNew.")

        assert "not a heading, it is inside a fence" not in rewritten

    def test_replacing_a_part_the_file_does_not_hold_raises(self) -> None:
        """Silently appending would lose the rewrite with nothing said."""
        with pytest.raises(PartNotFound):
            spliced(MISUSE, "2.3.9 Nothing", "text")


ABBREVIATIONS = """\
<!-- Abbreviations -->

*[ASI]: Artificial Superintelligence.
*[Superintelligence]: An AI with cognitive abilities far greater than humans'.
*[superintelligent]: An AI with cognitive abilities far greater than humans'.

not an entry at all
"""


class TestTheWorksOwnVocabularyIsTheGraph:
    """An imported work arrives with the authors' declared terms, so the
    dependency graph is dense before any run has coined anything."""

    def test_entries_are_read_and_other_lines_are_not(self) -> None:
        entries = abbreviations_in(ABBREVIATIONS)

        assert [entry.term for entry in entries] == [
            "ASI",
            "Superintelligence",
            "superintelligent",
        ]

    def test_a_part_using_a_term_depends_on_it(self) -> None:
        entries = abbreviations_in(ABBREVIATIONS)
        used = terms_used("The risk from ASI is the subject here.", entries)

        assert [held.subject for held in used] == ["ASI"]

    def test_a_term_inside_a_longer_word_is_not_used(self) -> None:
        """mkdocs substitutes at word boundaries; matching wider invents edges."""
        entries = abbreviations_in(ABBREVIATIONS)

        assert terms_used("The ASIMOV benchmark.", entries) == ()

    def test_spellings_the_authors_declared_apart_stay_apart(self) -> None:
        """They are separate entries because the published book substitutes
        each where it appears; folding them reports edges the work lacks."""
        entries = abbreviations_in(ABBREVIATIONS)
        used = terms_used("A superintelligent system.", entries)

        assert [held.subject for held in used] == ["superintelligent"]

    def test_a_work_declaring_no_vocabulary_is_not_an_error(
        self, tmp_path: Path
    ) -> None:
        assert read_abbreviations(tmp_path / "nothing.md") == ()


class TestAdoptingAWorkIsWhatMakesTheLoopAffordable:
    """Two hundred parts nothing has built is two hundred parts to rewrite
    before the loop can say anything useful. Adoption is `make -t`."""

    def test_adoption_settles_a_work_nothing_has_run_against(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)

        assert sweep(WorkState(), held).dirty()
        assert sweep(adopted(WorkState(), held), held).settled()

    def test_adoption_leaves_an_already_built_part_alone(self, tmp_path: Path) -> None:
        """A run's own record of what it read beats what the prose implies."""
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)
        recorded = WorkState().built(
            "02/03/2.3.2",
            source=next(r.text for r in held if r.node.key == "02/03/2.3.2"),
            consumed=consumption_of([MESA]),
            run="an-earlier-run",
        )
        after = adopted(recorded, held)

        stamp = after.stamp("02/03/2.3.2")
        assert stamp is not None
        assert stamp.run == "an-earlier-run"
        assert stamp.consumed.touches(MESA)

    def test_a_change_reaches_across_chapters(self, tmp_path: Path) -> None:
        """Lateral propagation, which is the whole point of the ledger."""
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)
        state = adopted(WorkState(), held).changed(
            "other/part", [ProposedChange(dependency=MESA)]
        )

        assert sweep(state, held).settled()

        leaning = tuple(
            PartReading(
                node=reading.node,
                text=reading.text,
                consumed=consumption_of([MESA]),
            )
            for reading in held
        )
        reached = sweep(
            adopted(WorkState(), leaning).changed(
                "other/part", [ProposedChange(dependency=MESA)]
            ),
            leaning,
        )
        assert len(reached.dirty()) == len(leaning)


class TestAWorkOutlivesEveryRunThatTouchedIt:
    """State kept in inkwell's own store, never in somebody else's checkout."""

    def test_a_work_round_trips_through_the_store(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        work = read_manuscript(atlas_like(tmp_path))
        store.publish_tree("atlas", work)
        state = adopted(WorkState(), readings(work))
        store.publish_state("atlas", state)

        again = ManuscriptStore(root=tmp_path / "manuscripts")
        assert again.works() == ("atlas",)
        assert again.load_tree("atlas") == work
        assert len(again.load_state("atlas").stamps) == len(list(work.leaves()))

    def test_a_work_nobody_imported_reads_as_absent(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path)

        assert store.load_tree("nothing") is None
        assert store.load_state("nothing") == WorkState()

    def test_each_part_coins_into_a_file_only_it_can_name(self, tmp_path: Path) -> None:
        """The partition, not a lock, is what makes concurrent parts safe."""
        store = ManuscriptStore(root=tmp_path)
        one = store.glossary_path("atlas", "02/03/2.3.2")
        other = store.glossary_path("atlas", "02/03/2.3.1")

        assert one != other
        assert one.parent == other.parent


class TestAQuestionParksItsPartRatherThanFailingIt:
    """Parked work is sound and suspended; failed work is retried and fails
    again. A scheduler reads the difference off the standing."""

    def mailbox_at(self, root: Path) -> PartMailbox:
        return PartMailbox(root=root)

    def test_asking_the_same_thing_twice_files_one_question(
        self, tmp_path: Path
    ) -> None:
        """A part re-run after a failure asks what it asked before."""
        mailbox = self.mailbox_at(tmp_path)
        prompt = "Should this keep the 2024 figure or take the 2026 one?"
        for _ in range(2):
            mailbox.ask(
                PartQuestion(
                    id=question_id("02/03/2.3.2", prompt),
                    work="atlas",
                    asker="02/03/2.3.2",
                    prompt=prompt,
                )
            )

        assert len(mailbox.open()) == 1

    def test_an_answered_question_stops_being_open(self, tmp_path: Path) -> None:
        mailbox = self.mailbox_at(tmp_path)
        identifier = question_id("02/03/2.3.2", "Which figure?")
        mailbox.ask(
            PartQuestion(
                id=identifier,
                work="atlas",
                asker="02/03/2.3.2",
                prompt="Which figure?",
            )
        )
        assert mailbox.answer(PartAnswer(id=identifier, value="the 2026 one"))

        assert mailbox.open() == ()
        settled = mailbox.settled(identifier)
        assert settled is not None
        assert settled.value == "the 2026 one"

    def test_a_second_answer_is_refused_rather_than_overwriting(
        self, tmp_path: Path
    ) -> None:
        """The part was rebuilt against the first; a revised answer would
        leave the work built on something nobody said."""
        mailbox = self.mailbox_at(tmp_path)
        identifier = question_id("02/03/2.3.2", "Which figure?")
        mailbox.ask(
            PartQuestion(
                id=identifier,
                work="atlas",
                asker="02/03/2.3.2",
                prompt="Which figure?",
            )
        )
        mailbox.answer(PartAnswer(id=identifier, value="the 2026 one"))

        assert not mailbox.answer(PartAnswer(id=identifier, value="the 2024 one"))

    def test_a_question_escalates_to_the_part_above_it(self, tmp_path: Path) -> None:
        """A sibling cannot answer; the tree already knows who is above."""
        work = read_manuscript(atlas_like(tmp_path))

        assert escalated_to(work, "02/03/2.3.2") == "02/03"
        assert escalated_to(work, "02/03") == "02"
        assert escalated_to(work, "02") == ""


class TestAPassLeavesHeldWorkAlone:
    """Picking up a part another run holds duplicates it; picking up a parked
    part re-asks what is already asked and gets no further."""

    def swept(self, tmp_path: Path, state: WorkState) -> WorkSweep:
        work = read_manuscript(atlas_like(tmp_path))
        return sweep(state, readings(work))

    def test_everything_dirty_is_schedulable_by_default(self, tmp_path: Path) -> None:
        found = self.swept(tmp_path, WorkState())

        assert len(schedulable(found, WorkState())) == len(found.dirty())

    def test_a_running_part_is_not_picked_up_again(self, tmp_path: Path) -> None:
        state = WorkState().declared("02/03/2.3.2", "running", "held")
        found = self.swept(tmp_path, state)

        assert "02/03/2.3.2" not in [held.key for held in schedulable(found, state)]

    def test_a_parked_part_waits_rather_than_being_retried(
        self, tmp_path: Path
    ) -> None:
        state = WorkState().declared("02/03/2.3.2", "parked", "asked a question")
        found = self.swept(tmp_path, state)

        assert "02/03/2.3.2" not in [held.key for held in schedulable(found, state)]


class TestAPartIsWrittenAsTheWorkIsWrittenAs:
    """A part run's format comes from the work, and one run cannot answer it
    differently from the next.

    Worth pinning at the call rather than at the field: the defect this replaces
    was not a wrong format, it was no format — ``run_session`` defaulting to
    ``auto`` while nothing in the manuscript path passed one, so a subsection of
    a textbook was revised without the rules the textbook declares and nothing
    said so.
    """

    def test_a_work_of_chapters_is_a_textbook_unless_told_otherwise(
        self, tmp_path: Path
    ) -> None:
        assert read_manuscript(atlas_like(tmp_path)).target_format == "textbook"

    def test_the_format_is_what_the_import_was_given(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path), target_format="lesswrong")

        assert work.target_format == "lesswrong"

    def test_the_format_survives_the_store(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_tree(
            "w", read_manuscript(atlas_like(tmp_path), target_format="memo")
        )
        held = store.load_tree("w")

        assert held is not None and held.target_format == "memo"

    def test_a_work_recorded_before_formats_reads_as_a_textbook(self) -> None:
        """A tree.json written without the field is not a work with no format."""
        held = Manuscript.model_validate({"title": "t", "root": "/r", "children": []})

        assert held.target_format == "textbook"

    @pytest.mark.asyncio
    async def test_the_run_is_told_the_works_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chapters = atlas_like(tmp_path)
        work = read_manuscript(chapters, target_format="textbook")
        node = work.node("02/03/2.3.2")
        assert node is not None
        asked: dict[str, object] = {}

        async def record(**passed: object) -> AgentSessionResult:
            asked.update(passed)
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", record)
        await run_part(
            ManuscriptStore(root=tmp_path / "manuscripts"),
            "w",
            work,
            node,
            session_id="s",
            scratch=tmp_path / "room",
            agents=OFFLINE,
        )

        assert asked["target_format"] == "textbook"

    @pytest.mark.asyncio
    async def test_a_work_declaring_another_format_carries_that_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The work is asked, not the module default — otherwise the override is
        a field nothing reads."""
        chapters = atlas_like(tmp_path)
        work = read_manuscript(chapters, target_format="lesswrong")
        node = work.node("02/03/2.3.2")
        assert node is not None
        asked: dict[str, object] = {}

        async def record(**passed: object) -> AgentSessionResult:
            asked.update(passed)
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", record)
        await run_part(
            ManuscriptStore(root=tmp_path / "manuscripts"),
            "w",
            work,
            node,
            session_id="s",
            scratch=tmp_path / "room",
            agents=OFFLINE,
        )

        assert asked["target_format"] == "lesswrong"


class TestAnUndeclaredFormatIsRefusedWhereItIsTyped:
    """An unknown format carries no guidance and no checks, and nothing
    downstream refuses it — so the surface taking it from a person does."""

    def test_a_declared_format_passes(self) -> None:
        assert unknown_format("textbook") == ""

    def test_auto_passes_because_it_is_an_instruction_not_a_format(self) -> None:
        assert unknown_format("auto") == ""

    def test_a_custom_format_carrying_its_description_passes(self) -> None:
        assert unknown_format("custom: a field guide entry") == ""

    def test_a_typo_is_named_along_with_what_is_declared(self) -> None:
        refusal = unknown_format("textbok")

        assert "textbok" in refusal and "textbook" in refusal


class TestAFailedPartAlwaysSaysWhy:
    """A part recorded as failed for no stated reason is the thing somebody
    stares at wondering what they did wrong. The one exception that carries no
    message is the one a person causes: stopping a run stringifies to nothing,
    so the reason was empty exactly when somebody had just pressed a button and
    wanted to know what it did."""

    def recorded(self, tmp_path: Path, raised: BaseException) -> Recorded:
        """What the loop writes down for a turn that raised."""
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)
        state = adopted(WorkState(), held)
        found = sweep(state.declared("02/03/2.3.2", "requested", "sharpen"), held)
        verdict = next(held for held in found.verdicts if held.key == "02/03/2.3.2")
        return recording(
            state,
            verdict,
            raised,
            PartMailbox(root=tmp_path / "box"),
            work,
            "atlas",
            "run1",
        )

    def test_a_stopped_run_is_named_rather_than_left_blank(
        self, tmp_path: Path
    ) -> None:
        step = self.recorded(tmp_path, asyncio.CancelledError())

        assert step.result.detail == "CancelledError"
        assert step.state.record("02/03/2.3.2") is not None

    def test_a_run_that_said_why_keeps_its_words(self, tmp_path: Path) -> None:
        step = self.recorded(tmp_path, RuntimeError("the model went away"))

        assert step.result.detail == "the model went away"


class TestAPartRunCanReadTheWorkItIsIn:
    """A run was told it sat "among others that readers reach before and after
    it" and asked not to repeat them — a rule about text it had no way to see.
    It had its own span, the ancestors' titles, and the term ledger, and nothing
    else of the book. What that produces is a run which goes looking for the
    work where it can reach it: the published site, a different edition of the
    book it is holding one page of, whose chapters are numbered differently."""

    def test_the_neighbouring_parts_are_named_with_their_files(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        near = around(work, node)

        assert "02/03/2.3.1" in near
        assert "02/03/2.3.3" in near
        assert str(Path(work.root)) in near

    def test_the_first_part_has_only_what_follows_it(self, tmp_path: Path) -> None:
        """No part before it, and a run told about one would go looking."""
        work = read_manuscript(atlas_like(tmp_path))
        first = next(held for held in work.leaves() if held.path)

        near = around(work, first)

        assert first.key not in near
        assert len(near.splitlines()) == 1

    def test_the_instruction_says_the_work_is_readable(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        said = part_instruction(work, node)

        assert "you can read any of it" in said
        assert str(Path(work.root)) in said

    def test_a_part_the_work_does_not_hold_names_no_neighbours(
        self, tmp_path: Path
    ) -> None:
        """Rather than naming whichever parts happen to sit at index zero."""
        work = read_manuscript(atlas_like(tmp_path))
        stranger = ManuscriptNode(key="09/09/9.9.9", kind="section", title="Nowhere")

        assert around(work, stranger) == ""


class TestTheStandingTextIsMaterialRatherThanTheShape:
    """A run handed the standing text as the piece it is *replacing* keeps that
    piece's shape: seven headings survived a full rewrite in their original
    order with thirty-four new ones hung off them, and the most pressing
    development in the section landed fourth because a heading was already
    fourth. The concrete instruction beat the abstract warning beside it, which
    is why the fix is where the material is routed rather than in the wording.
    """

    @pytest.mark.asyncio
    async def test_the_part_reaches_the_run_as_material_to_write_from(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        asked: dict[str, object] = {}

        async def record(**passed: object) -> AgentSessionResult:
            asked.update(passed)
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", record)
        await run_part(
            ManuscriptStore(root=tmp_path / "manuscripts"),
            "atlas",
            work,
            node,
            session_id="s",
            agents=OFFLINE,
        )

        assert asked["material_role"] == "source"

    def test_the_instruction_does_not_ask_for_the_structure_to_be_kept(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        said = part_instruction(work, node)

        assert "structure" not in said
        assert "it is not the shape of what you are writing" in said

    def test_the_voice_is_still_the_authors(self, tmp_path: Path) -> None:
        """Only the shape stops being inherited. A part that came back in a
        different register would be a worse failure than one that kept its
        headings."""
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        assert "Keep the author's voice" in part_instruction(work, node)


class TestASelectedSubsectionPlansFromItsPushedEvidence:
    """A click on one part must not reuse a plan made before its evidence moved."""

    @pytest.mark.asyncio
    async def test_new_subsection_evidence_invalidates_the_stored_book_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        current = held_text(
            (Path(work.root) / node.path).read_text(encoding="utf-8"), node
        )
        stale = ComposedBrief(
            thesis="An outage is the main cyber risk.",
            opening=EditorialSection(
                title="The old outage",
                establishes="Historical context.",
                order_reason="The old plan inherited it first.",
                evidence=["The old outage report."],
                handoff="Continue to the current landscape.",
                key_points=[],
            ),
        )
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        old_evidence = evidence_for(work, node, WorkFindings())
        store.publish_briefs(
            "atlas",
            WorkBriefs(
                briefs=(
                    PartPlan(
                        key=node.key,
                        brief=stale,
                        digest=digest_of(current),
                        evidence_digest=old_evidence.digest(),
                    ),
                )
            ),
        )
        pushed = WorkFindings()

        class PushesCurrent(CorpusPusher):
            def __init__(self) -> None:
                self.topic = ""

            async def push(self, topic: str) -> tuple[PushedCorpusClaim, ...]:
                self.topic = topic
                return (
                    PushedCorpusClaim(
                        title="Mythos system card",
                        claim="A live agentic campaign crossed the full attack chain.",
                        source="mythos/system-card",
                    ),
                )

        class PlansCurrent(BriefWriter):
            def __init__(self) -> None:
                self.task = ""

            async def compose(self, task: str, root: Path) -> ComposedBrief:
                self.task = task
                return ComposedBrief(
                    thesis="Agentic intrusion changes the threat model.",
                    opening=EditorialSection(
                        title="The live agentic campaign",
                        establishes="The current evidence.",
                        order_reason="It is the pressing development.",
                        evidence=["Mythos system card."],
                        handoff="The history can now follow as context.",
                        key_points=[],
                    ),
                )

        planner = PlansCurrent()
        corpus = PushesCurrent()
        passed: dict[str, object] = {}

        async def record(**given: object) -> AgentSessionResult:
            passed.update(given)
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", record)
        await run_part(
            store,
            "atlas",
            work,
            node,
            session_id="s",
            found=pushed,
            agents=PartRunAgents(
                briefing=planner, corpus=corpus, inheriting=PassesThrough()
            ),
        )

        plan = passed["plan"]
        assert isinstance(plan, ArticlePlan)
        assert plan.sections[0].title == "The live agentic campaign"
        assert "full attack chain" in planner.task
        assert "Prose about cyber risk" not in planner.task
        assert "Cyber Risk" in corpus.topic
        assert "Prose about cyber risk" not in corpus.topic

    @pytest.mark.asyncio
    async def test_a_missing_prose_blind_brief_stops_before_the_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        class Declines(BriefWriter):
            async def compose(self, task: str, root: Path) -> None:
                return None

        called = False

        async def record(**given: object) -> AgentSessionResult:
            nonlocal called
            called = True
            raise AssertionError("writing pipeline must not start")

        monkeypatch.setattr(runner_module, "run_session", record)
        outcome = await run_part(
            ManuscriptStore(root=tmp_path / "manuscripts"),
            "atlas",
            work,
            node,
            session_id="s",
            agents=PartRunAgents(
                briefing=Declines(), corpus=PushesNothing(), inheriting=PassesThrough()
            ),
        )

        assert outcome.ended() == "failed"
        assert "pipeline not started" in outcome.failure
        assert not called

    @pytest.mark.asyncio
    async def test_briefing_is_visible_before_the_pipeline_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        class Reports(PlansPart):
            async def compose(self, task: str, root: Path) -> ComposedBrief:
                agent = client.context_value(client.active_agent_callback, None)
                blocks = client.context_value(client.active_block_callback, None)
                trace = client.context_value(client.active_trace_logger, None)
                assert agent is not None
                assert blocks is not None
                assert trace is not None
                trace.log_text("Planning the successor", heading="Brief")
                started = client.AgentUpdate(label="brief", address="brief")
                await agent(started)
                await blocks(TurnTextBlock(text="Planning the successor"), "brief")
                await agent(started.model_copy(update={"status": "completed"}))
                return await super().compose(task, root)

        class Records(PipelineListener):
            def __init__(self) -> None:
                super().__init__()
                self.agents: tuple[client.AgentUpdate, ...] = ()
                self.blocks: tuple[tuple[str, str], ...] = ()

            async def on_agent(self, update: client.AgentUpdate) -> None:
                self.agents = (*self.agents, update)

            async def on_block(
                self, block_type: str, content: str, prefix: str
            ) -> None:
                self.blocks = (*self.blocks, (content, prefix))

        async def raw_draft(**passed: object) -> AgentSessionResult:
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", raw_draft)
        listener = Records()
        observers = PartRunObservers(listener=listener)
        await run_part(
            ManuscriptStore(root=tmp_path / "manuscripts"),
            "atlas",
            work,
            node,
            session_id="s",
            observers=observers,
            agents=PartRunAgents(
                briefing=Reports(), corpus=PushesNothing(), inheriting=PassesThrough()
            ),
        )

        assert [one.status for one in listener.agents] == ["running", "completed"]
        assert listener.blocks == (("Planning the successor", "brief"),)
        trace_path = observers.trace.save()
        assert trace_path is not None
        assert "Planning the successor" in trace_path.read_text(encoding="utf-8")


class TestTheReasonsAreWhatLicenseTheScope:
    """Nothing in a part run is in a position to decide what the subsection
    should open with. What asked for the run is."""

    def test_the_reasons_are_named_as_the_scope(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        said = part_instruction(
            work, node, ("the CrowdStrike figure is misattributed",)
        )

        assert "the CrowdStrike figure is misattributed" in said
        assert "That list is the scope" in said

    def test_a_part_with_nothing_outstanding_is_told_so(self, tmp_path: Path) -> None:
        """A writer told only to write infers a licence from the size of the
        subject, and the subject is inexhaustible."""
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        assert "Nothing specific is outstanding" in part_instruction(work, node)


class TestFiguresReachTheWriterAsConstraints:
    """Figure 2.13 is the thirteenth figure of chapter two because twelve come
    before it in chapters this run cannot see."""

    def figured(self, tmp_path: Path) -> str:
        """The instruction for a part carrying one numbered figure."""
        chapters = atlas_like(tmp_path)
        held = (chapters / "02" / "03.md").read_text(encoding="utf-8")
        (chapters / "02" / "03.md").write_text(held + FIGURE_BLOCK, encoding="utf-8")
        work = read_manuscript(chapters)
        node = work.node("02/03/2.3.3")
        assert node is not None
        text = (chapters / "02" / "03.md").read_text(encoding="utf-8")
        return part_instruction(work, node, figures=inventory_of(text).figures)

    def test_the_number_the_book_gave_it_is_stated(self, tmp_path: Path) -> None:
        assert "Figure 2.14" in self.figured(tmp_path)

    def test_the_image_path_is_stated(self, tmp_path: Path) -> None:
        assert "Images/1Wx_Image_14.png" in self.figured(tmp_path)

    def test_renumbering_is_refused_rather_than_left_to_judgement(
        self, tmp_path: Path
    ) -> None:
        assert "Never renumber one" in self.figured(tmp_path)

    def test_a_part_with_no_figures_says_nothing_about_them(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        assert "Never renumber one" not in part_instruction(work, node)
        assert "numbering is the book's" not in part_instruction(work, node)


class TestAPartIsToldHowLongItsSiblingsRun:
    """Asked how long a subsection should be, a run holding that subsection and
    nothing else answers from the subject — and the subject is inexhaustible,
    so the answer is "longer". A thousand words in, eight thousand out."""

    def test_the_budget_names_what_the_part_holds_now(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        held = budget_for(work, node)

        assert held.holds == len(
            held_text(
                (Path(work.root) / node.path).read_text(encoding="utf-8"), node
            ).split()
        )

    def test_the_chapter_is_what_the_part_is_measured_against(
        self, tmp_path: Path
    ) -> None:
        """A subsection three times the length of every other subsection in its
        chapter is out of scale whatever the rest of the book does."""
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None

        held = budget_for(work, node)

        assert held.chapter == work.children[0].title
        assert held.siblings > 1
        assert held.chapter_words >= held.holds

    def test_the_room_either_side_is_the_declared_allowance(
        self, tmp_path: Path
    ) -> None:
        held = LengthBudget(holds=1000, allowance=1.5)

        assert (held.floor(), held.ceiling()) == (666, 1500)

    def test_the_budget_reads_as_a_delivery_contract(self) -> None:
        said = LengthBudget(holds=1000, chapter="Chapter 02", siblings=3).render()

        assert "accepted only inside that interval" in said

    def test_a_part_with_no_text_is_given_no_budget(self) -> None:
        """Nothing to be measured against, and a range around zero would read
        as an instruction to write nothing."""
        assert LengthBudget().render() == ""


class TestAPartRunSharesTheWorksTermLedger:
    """The vocabulary has to reach the writers while they write.

    Worth pinning at the call: the defect this replaces was a run whose writers
    coined into a file under the session's notes while the harvest that carried
    those terms into the work read a path under the work's own store. Nothing
    was ever at that path, so the work's vocabulary never grew, and a part told
    to look up what the work already calls something was reaching into an empty
    file — a rule with no data behind it.
    """

    def part_of(self, tmp_path: Path) -> tuple[ManuscriptStore, Manuscript, str]:
        """A work in a store, and the key of the part these tests revise."""
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        work = read_manuscript(atlas_like(tmp_path))
        return store, work, "02/03/2.3.2"

    def declare(self, store: ManuscriptStore, term: str, meaning: str) -> None:
        """What the authors of the work spelled out before any run existed."""
        path = store.glossary_path("atlas", DECLARED_VOCABULARY)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_chapter_glossary(
            path,
            ChapterGlossary(terms=[GlossaryEntry(term=term, meaning=meaning)]),
        )

    @pytest.mark.asyncio
    async def test_the_run_is_handed_the_works_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, work, key = self.part_of(tmp_path)
        node = work.node(key)
        assert node is not None
        asked: dict[str, object] = {}

        async def record(**passed: object) -> AgentSessionResult:
            asked.update(passed)
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", record)
        await run_part(store, "atlas", work, node, session_id="s", agents=OFFLINE)

        scope = asked["glossary"]
        assert isinstance(scope, NodeGlossary)
        assert scope.own() == store.glossary_path("atlas", key)

    def test_the_authors_vocabulary_answers_a_writer_before_a_run_coins(
        self, tmp_path: Path
    ) -> None:
        """The read that a writer's lookup goes through, on a work no run has
        touched: an empty answer here is what let a part rename what the book
        had already settled."""
        store, _, key = self.part_of(tmp_path)
        self.declare(store, "Superintelligence", "Capability past every human.")

        held = read_glossary(ledger(store, "atlas", key))

        assert [entry.term for entry in held.terms] == ["Superintelligence"]

    def test_the_declared_file_is_read_before_anything_a_part_coined(
        self, tmp_path: Path
    ) -> None:
        """First-definition-wins makes the authors' spelling bind, and only the
        read order says the authors go first."""
        store, _, key = self.part_of(tmp_path)
        self.declare(store, "Superintelligence", "The authors' meaning.")
        write_chapter_glossary(
            store.glossary_path("atlas", "02/03/2.3.1"),
            ChapterGlossary(
                terms=[GlossaryEntry(term="Superintelligence", meaning="A run's.")]
            ),
        )

        first = read_glossary(ledger(store, "atlas", key)).terms[0]

        assert first.meaning == "The authors' meaning."

    def test_a_term_a_run_coined_reaches_the_parts_leaning_on_it(
        self, tmp_path: Path
    ) -> None:
        store, _, key = self.part_of(tmp_path)
        scope = ledger(store, "atlas", key)
        scope.own().parent.mkdir(parents=True, exist_ok=True)
        before = named_in(scope)
        write_chapter_glossary(
            scope.own(),
            ChapterGlossary(
                terms=[GlossaryEntry(term="Sharp Left Turn", meaning="A.")]
            ),
        )

        moved = coinages(scope, before, key)

        assert [change.dependency.subject for change in moved] == ["Sharp Left Turn"]
        assert "coined" in moved[0].detail

    def test_a_term_the_part_already_held_moves_nothing(self, tmp_path: Path) -> None:
        """What keeps the loop settling. The write stage reseeds this file every
        run, so a part that coins the same term again has moved nothing — and
        saying it had would dirty everything downstream on every pass."""
        store, _, key = self.part_of(tmp_path)
        scope = ledger(store, "atlas", key)
        scope.own().parent.mkdir(parents=True, exist_ok=True)
        settled = ChapterGlossary(
            terms=[GlossaryEntry(term="Sharp Left Turn", meaning="One meaning.")]
        )
        write_chapter_glossary(scope.own(), settled)
        before = named_in(scope)
        write_chapter_glossary(scope.own(), settled)

        assert coinages(scope, before, key) == ()

    def test_a_meaning_the_run_replaced_is_a_change(self, tmp_path: Path) -> None:
        """Downstream parts lean on what a term means, so a name kept over a
        meaning replaced is the change they most need to hear."""
        store, _, key = self.part_of(tmp_path)
        scope = ledger(store, "atlas", key)
        scope.own().parent.mkdir(parents=True, exist_ok=True)
        write_chapter_glossary(
            scope.own(),
            ChapterGlossary(terms=[GlossaryEntry(term="Deception", meaning="Old.")]),
        )
        before = named_in(scope)
        write_chapter_glossary(
            scope.own(),
            ChapterGlossary(terms=[GlossaryEntry(term="Deception", meaning="New.")]),
        )

        moved = coinages(scope, before, key)

        assert [change.dependency.subject for change in moved] == ["Deception"]
        assert "redefined" in moved[0].detail


class TestARewriteThatLostItsHeadingIsRefused:
    """Spliced in, prose that dropped its heading leaves the file with no part
    where the tree records one: the part reads as text the author deleted, and
    the prose itself has merged into whichever part precedes it. Nothing looks
    wrong afterwards, which is why it is checked before the write rather than
    reported after it."""

    def test_a_replacement_without_the_heading_is_refused(self) -> None:
        with pytest.raises(HeadingLost):
            spliced(MISUSE, "2.3.2 Cyber Risk {: #02}", "Rewritten prose.\n")

    def test_prose_before_the_heading_is_refused(self) -> None:
        """Leading prose splices in above the heading, where the file gives it
        to the part before rather than to this one."""
        with pytest.raises(HeadingLost):
            spliced(
                MISUSE,
                "2.3.2 Cyber Risk {: #02}",
                "Here is the revision.\n\n## 2.3.2 Cyber Risk\n\nProse.\n",
            )

    def test_a_heading_inside_a_fence_does_not_count_as_keeping_it(self) -> None:
        with pytest.raises(HeadingLost):
            spliced(
                MISUSE,
                "2.3.2 Cyber Risk {: #02}",
                "```\n## 2.3.2 Cyber Risk\n```\n",
            )

    def test_a_rewrite_of_another_part_is_refused(self) -> None:
        """The one that would otherwise land silently: valid markdown, a real
        heading, and the wrong part."""
        with pytest.raises(HeadingLost):
            spliced(
                MISUSE,
                "2.3.2 Cyber Risk {: #02}",
                "## 2.3.1 Bio Risk\n\nProse.\n",
            )

    def test_a_replacement_that_kept_its_heading_is_spliced(self) -> None:
        held = spliced(
            MISUSE, "2.3.2 Cyber Risk {: #02}", "## 2.3.2 Cyber Risk\n\nNew.\n"
        )

        assert "New." in held
        assert "## 2.3.1 Bio Risk {: #01}" in held

    def test_the_anchor_the_author_wrote_need_not_come_back(self) -> None:
        """Matched on the title, as everything else about a part is: the
        attribute list is presentation, and refusing on it would fail every
        rewrite that rendered the heading plainly."""
        assert opens_with("## 2.3.2 Cyber Risk\n", "2.3.2 Cyber Risk {: #02}")

    @pytest.mark.asyncio
    async def test_the_part_keeps_its_text_and_the_run_says_what_happened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chapters = atlas_like(tmp_path)
        work = read_manuscript(chapters)
        node = work.node("02/03/2.3.2")
        assert node is not None
        before = (chapters / "02" / "03.md").read_text(encoding="utf-8")

        async def unheaded(**passed: object) -> AgentSessionResult:
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="Prose with no heading.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", unheaded)
        outcome = await run_part(
            ManuscriptStore(root=tmp_path / "manuscripts"),
            "atlas",
            work,
            node,
            session_id="s",
            agents=OFFLINE,
        )

        assert outcome.ended() == "failed"
        assert "2.3.2 Cyber Risk" in outcome.failure
        assert (chapters / "02" / "03.md").read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    async def test_what_a_run_wrote_survives_a_splice_that_refused_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prose is kept before it is placed, so the one copy of hours of
        writing does not depend on the step after it succeeding."""
        chapters = atlas_like(tmp_path)
        work = read_manuscript(chapters)
        node = work.node("02/03/2.3.2")
        assert node is not None

        async def unheaded(**passed: object) -> AgentSessionResult:
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="Prose with no heading.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", unheaded)
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        outcome = await run_part(
            store, "atlas", work, node, session_id="s", agents=OFFLINE
        )

        assert outcome.ended() == "failed"
        kept = store.work_dir("atlas") / "runs" / "s" / PRODUCED_FILE
        assert kept.read_text(encoding="utf-8") == "Prose with no heading.\n"


class TestTheAdoptedSuccessorOwnsTheLiveHandoff:
    """The pipeline draft is provisional until inheritance settles it."""

    @pytest.mark.asyncio
    async def test_the_live_final_receives_adopted_not_produced_prose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        node = work.node("02/03/2.3.2")
        assert node is not None
        adopted = "## 2.3.2 Cyber Risk\n\nSettled successor.\n"

        class Settles(InheritanceReader):
            async def read(self, task: str, room: Path) -> Audit:
                (room / ADOPTED_FILE).write_text(adopted, encoding="utf-8")
                return Audit()

        class Records(PartPublisher):
            def __init__(self) -> None:
                self.published: list[tuple[str, str]] = []

            async def publish(self, doc_id: str, text: str) -> None:
                self.published.append((doc_id, text))

        async def raw_draft(**passed: object) -> AgentSessionResult:
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(
                    title="",
                    content="## 2.3.2 Cyber Risk\n\nRaw pipeline draft.\n",
                    google_doc_id="doc-7",
                ),
            )

        monkeypatch.setattr(runner_module, "run_session", raw_draft)
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        publisher = Records()

        outcome = await run_part(
            store,
            "atlas",
            work,
            node,
            session_id="s",
            agents=PartRunAgents(
                briefing=PlansPart(), corpus=PushesNothing(), inheriting=Settles()
            ),
            publisher=publisher,
        )

        assert outcome.text == adopted
        assert publisher.published == [("doc-7", adopted)]
        room = store.work_dir("atlas") / "runs" / "s"
        handoff = PartHandoff.model_validate_json(
            (room / HANDOFF_FILE).read_text(encoding="utf-8")
        )
        assert handoff.working_doc == "doc-7"
        assert handoff.adopted_digest == digest_of(adopted)
        assert "Settled successor" in (Path(work.root) / node.path).read_text(
            encoding="utf-8"
        )


class TestAPassSaysWhatItIsDoingWhileItIsDoingIt:
    """A pass over a book is long, and its report exists once it is over, so
    anything that wanted to show what was being written had nothing to show.
    The watch is that signal, and what matters about it is that it closes: a
    run reported as open and never closed reads as running for good."""

    def watcher(self) -> tuple[PartRunWatch, list[str]]:
        """A watch that writes down what it was told, in order."""
        said: list[str] = []

        class Noting(PartRunWatch):
            def opening(self, key: str, session: str) -> PartRunObservers:
                said.append(f"open {key}")
                return PartRunObservers()

            def closed(self, key: str, session: str, outcome: PartOutcome) -> None:
                said.append(f"closed {key} {outcome.ended()}")

        return Noting(), said

    @pytest.mark.asyncio
    async def test_a_part_run_is_opened_and_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        watch, said = self.watcher()

        async def record(**passed: object) -> AgentSessionResult:
            return AgentSessionResult(
                session_id="s",
                timestamp="",
                output=WritingOutput(title="", content="## 2.3.2 Cyber Risk\n\nNew.\n"),
            )

        monkeypatch.setattr(runner_module, "run_session", record)
        store, work = self.staged(tmp_path)

        await run_pass(
            store,
            "atlas",
            work,
            only=("02/03/2.3.2",),
            watching=watch,
            agents=OFFLINE,
        )

        assert said == ["open 02/03/2.3.2", "closed 02/03/2.3.2 rewritten"]

    @pytest.mark.asyncio
    async def test_a_run_that_raises_is_still_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case that would otherwise leave a session reading as running
        for as long as the server was up, having ended in the first minute."""
        watch, said = self.watcher()

        async def broken(**passed: object) -> AgentSessionResult:
            raise RuntimeError("the model went away")

        monkeypatch.setattr(runner_module, "run_session", broken)
        store, work = self.staged(tmp_path)

        await run_pass(
            store,
            "atlas",
            work,
            only=("02/03/2.3.2",),
            watching=watch,
            agents=OFFLINE,
        )

        assert said == ["open 02/03/2.3.2", "closed 02/03/2.3.2 failed"]

    def staged(self, tmp_path: Path) -> tuple[ManuscriptStore, Manuscript]:
        """A work with one part asked for, ready for a pass to pick it up."""
        work = read_manuscript(atlas_like(tmp_path))
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_tree("atlas", work)
        state = adopted(WorkState(), readings(work))
        store.publish_state(
            "atlas", state.declared("02/03/2.3.2", "requested", "sharpen it")
        )
        return store, work


class TestAPassCanBeKeptToOnePart:
    """Revising one subsection is the thing an author asks for most, and a limit
    cannot express it: a limit takes the first parts in tree order, so asking
    for one part by naming it and cutting the pass to one runs whichever part
    happens to come first in the book."""

    def sweep_of(self, tmp_path: Path) -> tuple[WorkSweep, WorkState]:
        """A work where two parts are outstanding and the rest are settled."""
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)
        state = adopted(WorkState(), held)
        state = state.declared("02/03/2.3.1", "requested", "tighten it")
        state = state.declared("02/03/2.3.3", "requested", "and this one")
        return sweep(state, held), state

    def test_naming_nothing_is_the_whole_sweep(self, tmp_path: Path) -> None:
        found, state = self.sweep_of(tmp_path)
        picked = schedulable(found, state)

        assert narrowed(picked, ()) == picked

    def test_naming_one_part_leaves_the_others(self, tmp_path: Path) -> None:
        found, state = self.sweep_of(tmp_path)

        picked = narrowed(schedulable(found, state), ("02/03/2.3.3",))

        assert [verdict.key for verdict in picked] == ["02/03/2.3.3"]

    def test_a_named_part_keeps_the_reasons_it_would_have_had(
        self, tmp_path: Path
    ) -> None:
        """Narrowing the sweep rather than overriding it is what carries the
        reasons through: a part run without them is revised on general
        principle."""
        found, state = self.sweep_of(tmp_path)

        picked = narrowed(schedulable(found, state), ("02/03/2.3.1",))

        assert picked[0].reasons == ("tighten it",)

    def test_a_named_part_that_is_up_to_date_is_reported_rather_than_run(
        self, tmp_path: Path
    ) -> None:
        found, state = self.sweep_of(tmp_path)

        passed = unpicked(found, state, ("02/03/2.3.2",))

        assert [held.key for held in passed] == ["02/03/2.3.2"]
        assert passed[0].reason == "up to date"

    def test_a_part_the_work_does_not_have_is_reported_as_such(
        self, tmp_path: Path
    ) -> None:
        """A mistyped key is the likeliest way to name a part, and it looks
        exactly like a settled work from a pass that ran nothing."""
        found, state = self.sweep_of(tmp_path)

        passed = unpicked(found, state, ("02/03/9.9.9",))

        assert passed[0].reason == "not a part of this work"

    def test_a_part_another_run_holds_says_so(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)
        state = adopted(WorkState(), held)
        state = state.declared("02/03/2.3.1", "running", "picked up by a pass", "s1")

        passed = unpicked(sweep(state, held), state, ("02/03/2.3.1",))

        assert "running" in passed[0].reason

    def test_a_part_parked_on_a_question_is_not_re_asked(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = readings(work)
        state = adopted(WorkState(), held)
        state = state.declared("02/03/2.3.1", "parked", "waiting on an answer", "s1")

        passed = unpicked(sweep(state, held), state, ("02/03/2.3.1",))

        assert "parked" in passed[0].reason

    def test_naming_a_part_nothing_asked_for_runs_nothing(self, tmp_path: Path) -> None:
        """Naming a part is not itself a request. Two ways to ask for a revision
        would disagree about why the part was being revised, so the door to a
        settled part stays `request`."""
        found, state = self.sweep_of(tmp_path)

        assert narrowed(schedulable(found, state), ("02/03/2.3.2",)) == ()
