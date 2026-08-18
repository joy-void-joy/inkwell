"""Reading a work of many parts into the tree a run works against.

What is worth pinning is what a filesystem walk would get wrong and the
declared order gets right, that the tree goes one level past the files because
that is where the revisable unit lives, and that a key survives the kinds of
edit an author actually makes — since state is keyed on it and a key that
moved is state that was lost.
"""

from pathlib import Path

from inkwell.manuscript.ingest import (
    heading_titles,
    nav_entries,
    ordinal_of,
    read_manuscript,
)
from inkwell.manuscript.tree import Manuscript, ManuscriptNode, WorkState

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

    def test_an_untouched_part_reads_clean(self) -> None:
        assert WorkState().status("02/03/2.3.2") == "clean"

    def test_marking_a_part_replaces_its_entry(self) -> None:
        state = WorkState().marked("02/03/2.3.2", "dirty", "the Hugging Face story")
        moved = state.marked("02/03/2.3.2", "running", "picked up")

        assert moved.status("02/03/2.3.2") == "running"
        assert len(moved.states) == 1

    def test_pending_is_what_a_pass_has_left(self) -> None:
        state = WorkState().marked("02/03/2.3.2", "dirty", "stale")

        assert [entry.key for entry in state.pending()] == ["02/03/2.3.2"]
        assert state.marked("02/03/2.3.2", "clean", "done").pending() == []


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
