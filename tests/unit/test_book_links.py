"""A chapter pointing at the book around it, and where that becomes a link.

A book is written one chapter per run, so the case these tests are mostly
about is the one that constraint forces rather than an edge: chapter three,
running alone, pointing at a chapter nothing has numbered. The writer has to
finish its section anyway, the document has to say so where the author is
reading, and the same sentence has to resolve to the right published page once
the book stage has named its target — without renumbering anything to fit.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from inkwell.agent import config as config_mod
from inkwell.agent.book import (
    BookLayout,
    BookRecord,
    BookStore,
    BookTarget,
    ChapterAddress,
    ChapterPlacement,
    ChapterRecord,
    ProposedChapter,
    SectionAddress,
)
from inkwell.agent.book_links import (
    MARKER_LABEL,
    BookReferences,
    parse_target,
    pointing,
)
from inkwell.agent.markdown_to_docs import DocsRequest
from inkwell.agent.session import WritingSessionState
from inkwell.agent.tools import google_docs
from inkwell.agent.tools.google_docs import CommentSpec, deliverable_requests

FOUNDATIONS = ProposedChapter(key="foundations", title="Foundations")
"""Chapter one: laid out, numbered, and there to be pointed at."""

CONSEQUENCES = ProposedChapter(key="consequences", title="Consequences")
"""The chapter a lone run points forward at before anything has numbered it."""


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BookStore:
    """The book store every part of the run resolves, pointed at a temp root."""
    root = tmp_path / "books"
    monkeypatch.setattr(config_mod.settings, "books_path", str(root))
    return BookStore(root=root)


@pytest.fixture(autouse=True)
def raised_comments(monkeypatch: pytest.MonkeyPatch) -> list[CommentSpec]:
    """What the write filed with the author, kept here instead of sent to Drive."""
    filed: list[CommentSpec] = []

    async def file_them(
        _doc_id: str, comments: list[CommentSpec], **_rest: object
    ) -> list[str]:
        filed.extend(comments)
        return [str(index) for index, _ in enumerate(comments)]

    monkeypatch.setattr(google_docs, "do_insert_comments_batch", file_them)
    return filed


@pytest.fixture
def session() -> Iterator[WritingSessionState]:
    """A run writing a chapter of ``textbook``, as the write path reads it."""
    state = WritingSessionState()
    state.doc_id = "doc"
    state.book = "textbook"
    token = google_docs.SESSION_STATE_VAR.set(state)
    yield state
    google_docs.SESSION_STATE_VAR.reset(token)


def lay_out(store: BookStore, *chapters: ProposedChapter) -> None:
    """Give each chapter an ordinal, the way the book stage does."""
    store.publish_outline(
        store.load("textbook").relaid(BookLayout(chapters=list(chapters)))
    )


async def write_once(markdown: str) -> list[DocsRequest]:
    """One deliverable write of ``markdown``, as ``do_write_tab`` renders it."""
    return await deliverable_requests(markdown, "tab")


def written(requests: list[DocsRequest]) -> str:
    """The text one write puts into the tab."""
    return "".join(
        request["insertText"]["text"] for request in requests if "insertText" in request
    )


def linked(requests: list[DocsRequest]) -> list[str]:
    """Every URL one write links to, in document order."""
    return [
        request["updateTextStyle"]["textStyle"]["link"]["url"]
        for request in requests
        if "updateTextStyle" in request
        and "link" in request["updateTextStyle"]["textStyle"]
    ]


class TestAReferenceNamesAKey:
    """Structured data naming a target, never a rendered link and never prose."""

    def test_a_chapter_is_named_by_its_key(self) -> None:
        assert parse_target("book:consequences") == BookTarget(chapter="consequences")

    def test_a_section_is_named_by_its_chapter_and_its_own_title(self) -> None:
        assert parse_target("book:measurement/the-error-budget") == BookTarget(
            chapter="measurement", section="the-error-budget"
        )

    def test_an_ordinary_link_is_not_a_reference(self) -> None:
        """Only the scheme makes one, so nothing else is reinterpreted."""
        assert parse_target("https://example.com/chapters/07") is None
        assert parse_target("/chapters/07") is None

    def test_a_target_the_book_has_no_address_for_is_not_one(self) -> None:
        assert parse_target("book:a/b/c") is None
        assert parse_target("book:") is None

    async def test_an_ordinary_link_survives_the_write_untouched(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        requests = await write_once("See [the paper](https://example.com/paper).")

        assert linked(requests) == ["https://example.com/paper"]


class TestTheWriterNeitherWaitsNorFabricates:
    """A target with no ordinal costs the writer nothing but the key."""

    async def test_the_section_is_written_whole(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        requests = await write_once(
            "Taken up in [the error budget](book:consequences)."
        )

        assert "Taken up in the error budget" in written(requests)

    async def test_no_ordinal_is_invented_for_it(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        """A fabricated number resolves to a real page that is the wrong one,
        which is the harm holding ordinals fixed exists to prevent."""
        requests = await write_once(
            "Taken up in [the error budget](book:consequences)."
        )

        assert linked(requests) == []

    async def test_the_book_is_not_numbered_to_suit_the_reference(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        lay_out(store, FOUNDATIONS)
        before = store.load("textbook").outline

        await write_once("Taken up in [the error budget](book:consequences).")

        assert store.load("textbook").outline == before


class TestAnUnresolvedReferenceSurvivesTheWrite:
    """Marked where the author is reading, raised where they answer."""

    async def test_the_prose_gains_a_marker_naming_what_it_wanted(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        requests = await write_once(
            "Taken up in [the error budget](book:consequences)."
        )

        assert f"[{MARKER_LABEL}: consequences]" in written(requests)

    async def test_the_same_marker_is_produced_for_every_kind_of_target(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        """One spelling, so the author finds all of them by looking for one."""
        requests = await write_once(
            "A [chapter](book:consequences) and a [section](book:consequences/why)."
        )

        text = written(requests)
        assert f"[{MARKER_LABEL}: consequences]" in text
        assert f"[{MARKER_LABEL}: consequences/why]" in text

    async def test_the_author_is_told_in_a_comment_as_well(
        self,
        store: BookStore,
        session: WritingSessionState,
        raised_comments: list[CommentSpec],
    ) -> None:
        """The document is a surface they follow live, so a marker they could
        scroll past is paired with a thread they answer whenever."""
        await write_once("Taken up in [the error budget](book:consequences).")

        assert len(raised_comments) == 1
        assert "consequences" in raised_comments[0].content
        assert raised_comments[0].anchor_text == "the error budget"

    async def test_one_target_is_raised_once_however_often_a_tab_is_rewritten(
        self,
        store: BookStore,
        session: WritingSessionState,
        raised_comments: list[CommentSpec],
    ) -> None:
        markdown = "Taken up in [the error budget](book:consequences)."

        await write_once(markdown)
        await write_once(markdown)

        assert len(raised_comments) == 1

    async def test_a_piece_belonging_to_no_book_resolves_nothing_and_still_writes(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        session.book = ""

        requests = await write_once(
            "Taken up in [the error budget](book:consequences)."
        )

        assert f"[{MARKER_LABEL}: consequences]" in written(requests)


class TestReconciliationAtTheDeliverableWrite:
    """The round trip a book written one chapter per run forces."""

    async def test_a_forward_reference_resolves_once_its_target_is_numbered(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        """A lone run points at a chapter nothing has numbered; the book stage
        names it later; the next write of that same sentence links to the page
        the chapter is published at."""
        lay_out(store, FOUNDATIONS)
        markdown = "Taken up in [the error budget](book:consequences)."

        before = await write_once(markdown)
        assert linked(before) == []
        assert f"[{MARKER_LABEL}: consequences]" in written(before)

        lay_out(store, FOUNDATIONS, CONSEQUENCES)
        after = await write_once(markdown)

        assert linked(after) == ["/chapters/02"]
        assert MARKER_LABEL not in written(after)

    async def test_the_book_is_re_read_at_each_write_rather_than_held(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        """The run writes into the same book it resolves against, so a chapter
        numbered since the last write has to be visible to this one."""
        assert linked(await write_once("[x](book:foundations)")) == []

        lay_out(store, FOUNDATIONS)

        assert linked(await write_once("[x](book:foundations)")) == ["/chapters/01"]

    async def test_a_section_resolves_to_the_section_page(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        lay_out(store, FOUNDATIONS)
        store.publish(
            ChapterRecord(
                placement=ChapterPlacement(book="textbook", chapter=1),
                title="Foundations",
                sections=["Why it holds", "The Calibration Curve"],
            )
        )

        requests = await write_once(
            "[the curve](book:foundations/the-calibration-curve)"
        )

        assert linked(requests) == ["/chapters/01/02"]

    async def test_a_section_its_chapter_has_not_recorded_stays_unresolved(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        """Falling back to the chapter index would land the reader on a real
        page that is not the one the sentence promised."""
        lay_out(store, FOUNDATIONS)

        requests = await write_once(
            "[the curve](book:foundations/the-calibration-curve)"
        )

        assert linked(requests) == []


class TestTheAddressDecidesTheUrl:
    """Derived from the ordinals, never kept beside them as a second string."""

    def test_both_published_forms_are_zero_padded_to_two_digits(self) -> None:
        assert ChapterAddress(chapter=1).path == "/chapters/01"
        assert SectionAddress(chapter=1, section=3).path == "/chapters/01/03"

    def test_the_path_and_the_file_stem_differ_only_in_separator(self) -> None:
        """The reader export keys its rows by one and serves pages at the
        other, so a padding drifting between them re-addresses filed feedback."""
        address = SectionAddress(chapter=1, section=3)

        assert address.key == ".".join(address.ordinals())
        assert address.path == "/chapters/" + "/".join(address.ordinals())

    def test_an_ordinal_already_two_digits_wide_is_not_padded_further(self) -> None:
        assert ChapterAddress(chapter=12).path == "/chapters/12"


class TestOrdinalsStayTheirOwnIdentity:
    """A reference resolves to what its target was assigned, whatever moved."""

    async def test_a_reference_follows_its_chapter_through_a_reorder(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        lay_out(store, FOUNDATIONS, CONSEQUENCES)
        lay_out(store, CONSEQUENCES, FOUNDATIONS)

        assert linked(await write_once("[x](book:consequences)")) == ["/chapters/02"]

    async def test_a_chapter_dropped_from_the_reading_order_resolves_to_nothing(
        self, store: BookStore, session: WritingSessionState
    ) -> None:
        """It keeps its ordinal so nobody is renumbered onto it, but nobody
        reads it either — a link into it is a link into a gap."""
        lay_out(store, FOUNDATIONS, CONSEQUENCES)
        lay_out(store, FOUNDATIONS)

        assert linked(await write_once("[x](book:consequences)")) == []


class TestWhatTheWriterIsShown:
    """The form, and this book's own keys, in the file it already reads."""

    def test_it_lists_the_keys_this_book_declared(self, store: BookStore) -> None:
        lay_out(store, FOUNDATIONS, CONSEQUENCES)

        shown = pointing(store.load("textbook"))

        assert "`foundations` — chapter 1 (Foundations)" in shown
        assert "`consequences` — chapter 2 (Consequences)" in shown

    def test_it_says_a_book_with_no_order_has_declared_no_keys(self) -> None:
        assert "declared no keys yet" in pointing(BookRecord(book="textbook"))

    def test_it_says_what_becomes_of_a_key_no_chapter_answers(
        self, store: BookStore
    ) -> None:
        """A writer told to point at what does not exist yet needs to know
        that is allowed, or it invents a number to avoid it."""
        lay_out(store, FOUNDATIONS)

        assert MARKER_LABEL in pointing(store.load("textbook"))


class TestTheResolverOnItsOwn:
    """The seam every call site shares, without a document around it."""

    def test_it_records_each_reference_it_could_not_place(self) -> None:
        references = BookReferences()

        references.destination("book:consequences", "the error budget")

        assert [held.target.spelled() for held in references.unresolved] == [
            "consequences"
        ]

    def test_it_records_nothing_for_a_link_it_did_not_touch(self) -> None:
        references = BookReferences()

        destination = references.destination("https://example.com", "the paper")

        assert destination.url == "https://example.com"
        assert references.unresolved == []
