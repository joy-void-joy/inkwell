"""One document per chapter, one tab per part, and comments that route home.

Four runs of one subsection made four documents; a pass over two hundred parts
makes two hundred, and the next makes two hundred more. What is worth pinning is
that the projection is idempotent — a chapter keeps its document and a tab is
written over rather than added beside — that a comment finds its part by the
passage it quotes rather than by a routing table, and that a comment taken in
once is not acted on again, which is what lets the loop settle.
"""

from pathlib import Path

import pytest

from inkwell.agent.tools import google_docs
from inkwell.agent.tools.google_docs import CommentEntry, CreatedDoc
from inkwell.manuscript import chapters as chapters_module
from inkwell.manuscript.chapters import (
    ChapterDoc,
    WorkDocs,
    carrying,
    chapters_of,
    held_text_of,
    parent_key,
    project,
    routed,
    unrouted,
)
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.store import ManuscriptStore

CHAPTER_PAGES = """\
nav:
- 2.3 - Misuse Risks: 03.md
title: 02 - Risks
"""

MISUSE = """\
# 2.3 Misuse Risks

## 2.3.1 Bio Risk

Prose about engineered pathogens.

## 2.3.2 Cyber Risk

Prose about the CrowdStrike outage and what it cost.
"""


def atlas_like(root: Path) -> Path:
    """A one-chapter work with two subsections under one section."""
    chapter = root / "chapters" / "02"
    chapter.mkdir(parents=True)
    (chapter / ".pages.yml").write_text(CHAPTER_PAGES, encoding="utf-8")
    (chapter / "03.md").write_text(MISUSE, encoding="utf-8")
    return root / "chapters"


class FakeDocs:
    """Google Docs as far as this module uses it, and no further.

    Records what was created and written so a test can ask whether a second
    projection made a second document, which is the whole question.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.tabs: dict[str, str] = {}
        self.written: dict[str, str] = {}
        self.comments: dict[str, list[CommentEntry]] = {}

    async def create_doc(self, title: str, **passed: object) -> CreatedDoc:
        self.created.append(title)
        held = f"doc-{len(self.created)}"
        return CreatedDoc(doc_id=held, url=f"https://docs.test/{held}")

    async def create_tab(
        self, doc_id: str, name: str, parent_tab_id: str | None = None
    ) -> str:
        held = f"{doc_id}:{parent_tab_id or 'root'}:{name}"
        self.tabs[held] = name
        return held

    async def write_tab(self, doc_id: str, tab_id: str, markdown: str) -> None:
        self.written[tab_id] = markdown

    async def fetch_comments(self, doc_id: str, **passed: object) -> list[CommentEntry]:
        # lup: ignore[dict-get] — keyed by whatever documents a test made
        return self.comments.get(doc_id, [])


@pytest.fixture
def docs(monkeypatch: pytest.MonkeyPatch) -> FakeDocs:
    """The Docs API, stubbed, so no test here reaches Google."""
    fake = FakeDocs()
    monkeypatch.setattr(chapters_module, "do_create_doc", fake.create_doc)
    monkeypatch.setattr(chapters_module, "do_create_tab", fake.create_tab)
    monkeypatch.setattr(chapters_module, "do_write_tab", fake.write_tab)
    monkeypatch.setattr(chapters_module, "do_fetch_comments", fake.fetch_comments)
    return fake


class TestTheWorksStructureIsTheDocumentsStructure:
    """Tabs nest three deep in Docs, which is exactly chapter, section,
    subsection — so nothing has to be laid out twice."""

    @pytest.mark.asyncio
    async def test_each_chapter_gets_a_document(
        self, tmp_path: Path, docs: FakeDocs
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        projected = await project(work, WorkDocs(), book=False)

        assert len(projected.chapters) == len(chapters_of(work))

    @pytest.mark.asyncio
    async def test_each_part_gets_a_tab(self, tmp_path: Path, docs: FakeDocs) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        projected = await project(work, WorkDocs(), book=False)
        chapter = projected.chapter("02")

        assert chapter is not None
        assert chapter.tab_for("02/03/2.3.2")

    @pytest.mark.asyncio
    async def test_a_subsections_tab_nests_under_its_sections(
        self, tmp_path: Path, docs: FakeDocs
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        projected = await project(work, WorkDocs(), book=False)
        chapter = projected.chapter("02")

        assert chapter is not None
        assert chapter.tab_for("02/03") in chapter.tab_for("02/03/2.3.2")

    @pytest.mark.asyncio
    async def test_a_tab_holds_its_own_parts_prose(
        self, tmp_path: Path, docs: FakeDocs
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        projected = await project(work, WorkDocs(), book=False)
        chapter = projected.chapter("02")

        assert chapter is not None
        written = docs.written[chapter.tab_for("02/03/2.3.2")]
        assert "CrowdStrike" in written
        assert "engineered pathogens" not in written

    def test_a_node_with_children_reads_whole(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        section = work.node("02/03")

        assert section is not None
        held = held_text_of(work, section)
        assert "engineered pathogens" in held
        assert "CrowdStrike" in held

    def test_the_parent_of_a_key_is_read_as_a_path(self) -> None:
        assert parent_key("02/03/2.3.2") == "02/03"
        assert parent_key("02") == ""


class TestProjectingTwiceMakesNothingTwice:
    """A chapter that has a document keeps it, and a tab that exists is written
    over rather than added beside — which is what makes this safe to run after
    every part lands."""

    @pytest.mark.asyncio
    async def test_a_second_projection_creates_no_second_document(
        self, tmp_path: Path, docs: FakeDocs
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        first = await project(work, WorkDocs(), book=True)
        made = len(docs.created)
        await project(work, first, book=True)

        assert len(docs.created) == made

    @pytest.mark.asyncio
    async def test_the_book_document_is_made_once(
        self, tmp_path: Path, docs: FakeDocs
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        first = await project(work, WorkDocs(), book=True)
        again = await project(work, first, book=True)

        assert again.book_doc_id == first.book_doc_id

    @pytest.mark.asyncio
    async def test_a_chapter_only_projection_makes_no_book_document(
        self, tmp_path: Path, docs: FakeDocs
    ) -> None:
        """Rebuilding a two-hundred-part document every time one part lands
        spends the projection on the surface fewest people look at."""
        work = read_manuscript(atlas_like(tmp_path))

        projected = await project(work, WorkDocs(), book=False)

        assert projected.book_doc_id == ""


class TestACommentFindsItsPartByWhatItQuotes:
    """A tab is a part, so which part an author is talking about is a lookup."""

    def test_a_quote_is_traced_to_the_part_holding_it(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert carrying(work, "the CrowdStrike outage") == "02/03/2.3.2"

    def test_wrapping_does_not_change_which_part_it_is(self, tmp_path: Path) -> None:
        """A document's line breaks are its own; a quote that came back wrapped
        differently is the same passage."""
        work = read_manuscript(atlas_like(tmp_path))

        assert carrying(work, "the CrowdStrike\n  outage") == "02/03/2.3.2"

    def test_a_quote_no_part_holds_traces_to_nothing(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert carrying(work, "a passage the author has since rewritten") == ""

    def test_an_unanchored_comment_traces_to_nothing(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))

        assert carrying(work, "") == ""


def commented(
    comment_id: str, content: str, quoted: str, *, resolved: bool = False
) -> CommentEntry:
    """One comment as Drive hands it back."""
    return CommentEntry(
        comment_id=comment_id,
        author="An author",
        content=content,
        anchor_text=quoted,
        resolved=resolved,
    )


class TestFeedbackReachesTheRunThatCanActOnIt:
    """And reaches it once: acted on twice, one comment asks for its part again
    on every sweep and the loop never settles."""

    def test_a_comment_is_addressed_to_the_part_it_quotes(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        held = routed(work, [commented("c1", "Wrong figure", "the CrowdStrike outage")])

        assert [one.key for one in held] == ["02/03/2.3.2"]

    def test_a_thread_arrives_as_one_note(self, tmp_path: Path) -> None:
        """A comment saying "this is wrong" and a reply saying "actually it is
        the figure" are the same instruction."""
        work = read_manuscript(atlas_like(tmp_path))
        entry = commented("c1", "This is wrong", "the CrowdStrike outage")
        entry = entry.model_copy(update={"replies": ["it is the figure"]})

        held = routed(work, [entry])

        assert "This is wrong / it is the figure" in held[0].said

    def test_a_comment_already_taken_in_is_not_taken_again(
        self, tmp_path: Path
    ) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        comments = [commented("c1", "Wrong figure", "the CrowdStrike outage")]

        assert routed(work, comments, seen=("c1",)) == ()

    def test_a_resolved_comment_asks_for_nothing(self, tmp_path: Path) -> None:
        work = read_manuscript(atlas_like(tmp_path))
        comments = [
            commented("c1", "Wrong figure", "the CrowdStrike outage", resolved=True)
        ]

        assert routed(work, comments) == ()

    def test_a_comment_no_part_claims_is_reported_rather_than_dropped(
        self, tmp_path: Path
    ) -> None:
        """Silently ignoring it loses exactly the feedback somebody took the
        trouble to leave."""
        work = read_manuscript(atlas_like(tmp_path))
        comments = [commented("c1", "What about this?", "text nobody holds")]

        assert routed(work, comments) == ()
        assert [one.comment_id for one in unrouted(work, comments)] == ["c1"]

    def test_taking_one_in_records_it(self) -> None:
        held = WorkDocs().taken_in(["c1", "c2"]).taken_in(["c1"])

        assert held.seen_comments == ("c1", "c2")


class TestOneWorkingDocumentPerPart:
    """Four runs of one subsection made four documents, and a pass makes two
    hundred more of them than it needs to."""

    def test_a_part_that_has_run_before_reuses_its_document(self) -> None:
        held = WorkDocs().with_working_doc("02/03/2.3.2", "doc-7")

        assert held.working_doc("02/03/2.3.2") == "doc-7"

    def test_a_part_that_has_never_run_has_none(self) -> None:
        assert WorkDocs().working_doc("02/03/2.3.2") == ""

    def test_running_it_again_replaces_rather_than_appends(self) -> None:
        held = WorkDocs().with_working_doc("02/03/2.3.2", "doc-7")

        again = held.with_working_doc("02/03/2.3.2", "doc-8")

        assert again.part_docs == (
            type(again.part_docs[0])(key="02/03/2.3.2", tab_id="doc-8"),
        )


class TestTheRecordSurvivesTheRunThatMadeIt:
    """A document id cannot be recomputed, and losing one leaves an orphan in
    somebody's Drive and makes a second one beside it."""

    def test_a_work_that_projects_nowhere_holds_nothing(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")

        assert store.load_docs("atlas").chapters == ()

    def test_what_was_projected_is_read_back(self, tmp_path: Path) -> None:
        store = ManuscriptStore(root=tmp_path / "manuscripts")
        store.publish_docs(
            "atlas",
            WorkDocs(
                chapters=(
                    ChapterDoc(key="02", doc_id="doc-1", url="https://d.test/1"),
                ),
                book_doc_id="doc-book",
            ),
        )

        held = store.load_docs("atlas")

        assert held.book_doc_id == "doc-book"
        assert held.chapter("02") is not None


def test_the_module_reaches_google_only_through_the_four_calls_it_stubs() -> None:
    """A test double is worth nothing if the module calls past it. These are
    the names the fixture replaces, and they have to be the ones it imports."""
    for name in ("do_create_doc", "do_create_tab", "do_write_tab", "do_fetch_comments"):
        assert getattr(chapters_module, name) is getattr(google_docs, name)
