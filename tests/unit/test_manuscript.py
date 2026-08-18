"""Reading a work of many parts into the tree a run works against.

What is worth pinning is what a filesystem walk would get wrong and the
declared order gets right, that the tree goes one level past the files because
that is where the revisable unit lives, and that a key survives the kinds of
edit an author actually makes — since state is keyed on it and a key that
moved is state that was lost.
"""

from pathlib import Path

import pytest

from inkwell.manuscript.facts import (
    Consumption,
    Dependency,
    ProposedChange,
    consumption_of,
)
from inkwell.manuscript.graph import PartReading, adopted, readings, sweep
from inkwell.manuscript.ingest import (
    heading_titles,
    nav_entries,
    ordinal_of,
    read_manuscript,
)
from inkwell.manuscript.splice import PartNotFound, held_text, spliced
from inkwell.manuscript.state import WorkState
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
