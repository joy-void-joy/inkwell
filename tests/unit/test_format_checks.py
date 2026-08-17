# claude: ignore
"""Tests for the checks a format declares beside its guidance."""

from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

import pytest

from inkwell.agent import config as config_mod
from inkwell.agent import format_checks
from inkwell.agent.book import (
    BookLayout,
    BookOutline,
    BookRecord,
    ChapterEntry,
    ChapterPlacement,
    ChapterRecord,
    CrossReference,
    ProposedChapter,
)
from inkwell.agent.book_links import BookReferences
from inkwell.agent.format_checks import (
    ADVISORY_PREAMBLE,
    BannedVocabulary,
    BlockLength,
    BoldedSummaries,
    BoldEmphasis,
    CheckRow,
    DraftWords,
    FormatCheck,
    FormulaicOpenings,
    Judge,
    JudgedRow,
    LinkingPredicates,
    MarkdownArtifacts,
    ParagraphLengthVariance,
    ParagraphSentences,
    ParticipialTails,
    PunctuationDensity,
    SectionLength,
    SentenceLengthVariance,
    UnresolvedReferences,
    Verdict,
    render_declared_rules,
    run_format_checks,
)
from inkwell.agent.models import ArticlePlan, SectionPlan
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import declared_format_checks
from inkwell.agent.segmenter import reader
from inkwell.agent.stages import format_checks_for, get_format_guidance


class StubJudge(Judge):
    """A reviewer whose verdict the test decides, recording what it was asked."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        self.asked: list[str] = []

    async def rule(self, question: str, draft_path: Path) -> Verdict:
        self.asked.append(question)
        return self.verdict


HELD = Verdict(holds=True, measured="nothing to report")


async def run_row(
    check: FormatCheck, draft: str, judge: Judge | None = None
) -> CheckRow:
    """One declared row's verdict on one draft."""
    report = await run_format_checks(
        draft,
        [check],
        format_key="test",
        draft_path=Path("draft.md"),
        judge=judge or StubJudge(HELD),
        reader=reader(),
    )
    return report.rows[0]


class TestBannedVocabulary:
    row = BannedVocabulary(
        name="banned", rule="No filler.", phrases=["it's worth noting", "crucial"]
    )

    async def test_fires_on_the_phrase(self) -> None:
        result = await run_row(self.row, "It's worth noting that the model scales.")
        assert result.fired
        assert result.findings

    async def test_passes_when_absent(self) -> None:
        result = await run_row(self.row, "The model scales with data.")
        assert not result.fired

    async def test_matches_words_not_substrings(self) -> None:
        result = await run_row(self.row, "Depth matters crucially for accuracy.")
        assert not result.fired

    async def test_ceiling_is_overridable(self) -> None:
        tolerant = self.row.model_copy(update={"ceiling": 1})
        result = await run_row(tolerant, "It's worth noting that the model scales.")
        assert not result.fired


class TestPunctuationDensity:
    async def test_fires_above_the_ceiling(self) -> None:
        row = PunctuationDensity(
            name="dashes", rule="Ration them.", marks=["—"], per_thousand_ceiling=1.0
        )
        result = await run_row(row, "The model — trained on text — scales — somehow.")
        assert result.fired

    async def test_passes_below_the_ceiling(self) -> None:
        row = PunctuationDensity(
            name="dashes", rule="Ration them.", marks=["—"], per_thousand_ceiling=500.0
        )
        result = await run_row(row, "The model — trained on text — scales.")
        assert not result.fired

    async def test_counts_only_the_declared_marks(self) -> None:
        """One kind serves every rationed mark; which one is data."""
        row = PunctuationDensity(
            name="semicolons",
            rule="Ration them.",
            marks=[";"],
            per_thousand_ceiling=1.0,
        )
        assert not (await run_row(row, "The model — trained on text — scales.")).fired
        assert (await run_row(row, "The model scales; the cost grows.")).fired

    async def test_parentheses_are_the_same_row_kind(self) -> None:
        row = PunctuationDensity(
            name="parens", rule="Ration them.", marks=["("], per_thousand_ceiling=1.0
        )
        result = await run_row(row, "The model scales (mostly) with data (so far).")
        assert result.fired

    async def test_a_mark_that_was_markup_is_not_counted(self) -> None:
        """Counted off the rendered prose, so a link target's paren is not one."""
        row = PunctuationDensity(
            name="parens", rule="Ration them.", marks=["("], per_thousand_ceiling=1.0
        )
        result = await run_row(
            row, "See [the entry](https://x.org/wiki/Set_(mathematics)) for the proof."
        )
        assert not result.fired


class TestMarkdownArtifacts:
    row = MarkdownArtifacts(name="artifacts", rule="No markdown left showing.")

    async def test_fires_on_an_unclosed_bold_run(self) -> None:
        result = await run_row(self.row, "The claim is **important but never closed.")
        assert result.fired
        assert result.findings

    async def test_fires_on_a_heading_with_no_space(self) -> None:
        result = await run_row(self.row, "##Heading that never parsed as one.")
        assert result.fired

    async def test_passes_when_the_markup_rendered(self) -> None:
        """Bold that parsed is formatting, not an artifact."""
        result = await run_row(self.row, "The claim is **important** and closed.")
        assert not result.fired

    async def test_passes_on_a_rendered_heading_and_link(self) -> None:
        result = await run_row(self.row, "## Heading\n\nSee [the paper](http://x).")
        assert not result.fired


class TestBoldEmphasis:
    async def test_fires_on_emphasis_through_the_body(self) -> None:
        row = BoldEmphasis(
            name="bold", rule="Bold sparingly.", per_thousand_ceiling=20.0
        )
        result = await run_row(row, "The **model** scales and the **cost** grows.")
        assert result.fired
        assert result.findings

    async def test_passes_on_unemphasized_prose(self) -> None:
        row = BoldEmphasis(
            name="bold", rule="Bold sparingly.", per_thousand_ceiling=20.0
        )
        result = await run_row(row, "The model scales and the cost grows.")
        assert not result.fired

    async def test_paragraph_summaries_are_exempt_where_declared(self) -> None:
        """The collision the author resolved: navigational bold is not overuse."""
        draft = "**Attention is expensive.** The cost grows with sequence length."
        strict = BoldEmphasis(
            name="bold", rule="Bold sparingly.", per_thousand_ceiling=0.0
        )
        exempting = strict.model_copy(update={"exempt_paragraph_summaries": True})
        assert (await run_row(strict, draft)).fired
        assert not (await run_row(exempting, draft)).fired

    async def test_the_exemption_does_not_cover_body_emphasis(self) -> None:
        row = BoldEmphasis(
            name="bold",
            rule="Bold sparingly.",
            per_thousand_ceiling=0.0,
            exempt_paragraph_summaries=True,
        )
        draft = "**Attention is expensive.** The **cost** grows with length."
        result = await run_row(row, draft)
        assert result.fired
        assert result.findings == ["cost"]


class TestFormulaicOpenings:
    row = FormulaicOpenings(name="openings", rule="Vary them.", repeat_ceiling=1)

    async def test_fires_when_an_opening_repeats(self) -> None:
        draft = (
            "Moreover, the models scale.\n\n"
            "Moreover, the costs grow.\n\n"
            "Moreover, the gains shrink."
        )
        result = await run_row(self.row, draft)
        assert result.fired
        assert result.findings

    async def test_passes_on_varied_openings(self) -> None:
        draft = (
            "Models scale with data.\n\n"
            "Costs grow with depth.\n\n"
            "Gains shrink at the margin."
        )
        result = await run_row(self.row, draft)
        assert not result.fired


class TestSentenceLengthVariance:
    row = SentenceLengthVariance(name="rhythm", rule="Vary length.", spread_floor=2.0)

    async def test_fires_on_uniform_sentences(self) -> None:
        draft = (
            "Models scale with data. Systems learn from text. Networks fail at math."
        )
        result = await run_row(self.row, draft)
        assert result.fired

    async def test_passes_on_varied_sentences(self) -> None:
        draft = (
            "Models scale. Training a transformer on a very large corpus of "
            "web text costs more money every year than the last."
        )
        result = await run_row(self.row, draft)
        assert not result.fired


class TestParagraphLengthVariance:
    row = ParagraphLengthVariance(
        name="paragraphs", rule="Vary length.", spread_floor=3.0
    )

    async def test_fires_on_uniform_paragraphs(self) -> None:
        draft = (
            "Models scale with data.\n\n"
            "Systems learn from text.\n\n"
            "Networks fail at math."
        )
        result = await run_row(self.row, draft)
        assert result.fired

    async def test_passes_on_varied_paragraphs(self) -> None:
        draft = (
            "Models scale.\n\n"
            "Training a transformer on a very large corpus of web text costs "
            "more money every single year than it did the year before, and "
            "the trend has not yet broken."
        )
        result = await run_row(self.row, draft)
        assert not result.fired


class TestLinkingPredicates:
    row = LinkingPredicates(name="linking", rule="Prefer verbs.", share_ceiling=0.0)

    async def test_fires_on_an_inflated_copula(self) -> None:
        result = await run_row(self.row, "The cache serves as a bottleneck.")
        assert result.fired
        assert result.findings

    async def test_fires_on_the_longest_enumerated_form(self) -> None:
        result = await run_row(
            self.row, "The dataset holds the distinction of being the largest."
        )
        assert result.fired

    async def test_passes_on_acting_verbs(self) -> None:
        draft = (
            "Attention costs quadratic time. The model reads every token. "
            "Training consumes power."
        )
        result = await run_row(self.row, draft)
        assert not result.fired

    async def test_a_plain_copula_is_not_the_target(self) -> None:
        """The row measures the inflated stand-ins, not every use of `is`."""
        result = await run_row(self.row, "Attention is expensive.")
        assert not result.fired

    async def test_the_phrase_list_is_overridable(self) -> None:
        row = self.row.model_copy(update={"phrases": ["plays host to"]})
        assert not (await run_row(row, "The cache serves as a bottleneck.")).fired
        assert (await run_row(row, "The cache plays host to the bottleneck.")).fired


class TestParticipialTails:
    row = ParticipialTails(name="tails", rule="No tails.", share_ceiling=0.0)

    async def test_fires_on_a_comma_gerund_tail(self) -> None:
        result = await run_row(
            self.row, "The model failed the benchmark, underscoring the gap."
        )
        assert result.fired
        assert result.findings

    async def test_passes_on_a_relative_clause(self) -> None:
        result = await run_row(
            self.row, "The model failed the benchmark, which underscores the gap."
        )
        assert not result.fired

    async def test_passes_on_a_gerund_it_does_not_watch(self) -> None:
        result = await run_row(
            self.row, "The model failed the benchmark, leaving the gap open."
        )
        assert not result.fired

    async def test_the_set_extends_without_being_restated(self) -> None:
        row = self.row.model_copy(update={"extra_gerunds": ["leaving"]})
        result = await run_row(
            row, "The model failed the benchmark, leaving the gap open."
        )
        assert result.fired
        assert all(
            gerund in row.gerunds
            for gerund in ("highlighting", "underscoring", "demonstrating")
        )

    async def test_a_mid_sentence_aside_is_not_a_tail(self) -> None:
        """The comma has to fall near the end, not anywhere in the sentence."""
        result = await run_row(
            self.row,
            "The model failed, underscoring a gap that every later run in the "
            "whole evaluation suite went on to reproduce without exception.",
        )
        assert not result.fired


class TestBoldedSummaries:
    async def test_fires_below_the_floor(self) -> None:
        row = BoldedSummaries(name="bold", rule="Bold each claim.", share_floor=1.0)
        draft = "Attention is expensive.\n\n**Cost** grows with length."
        result = await run_row(row, draft)
        assert result.fired
        assert result.findings

    async def test_passes_at_the_floor(self) -> None:
        row = BoldedSummaries(name="bold", rule="Bold each claim.", share_floor=1.0)
        draft = (
            "**Attention is expensive.** The cost grows with sequence length.\n\n"
            "**Caching helps.** Reused keys never get recomputed."
        )
        result = await run_row(row, draft)
        assert not result.fired

    async def test_fires_above_the_ceiling(self) -> None:
        """The same row states the memo rule, where uniform bolding is the fault."""
        row = BoldedSummaries(name="bold", rule="Bold sparingly.", share_ceiling=0.5)
        draft = (
            "**Attention is expensive.** The cost grows with sequence length.\n\n"
            "**Caching helps.** Reused keys never get recomputed."
        )
        result = await run_row(row, draft)
        assert result.fired


class TestParagraphSentences:
    row = ParagraphSentences(name="length", rule="Three to five.", floor=3, ceiling=5)

    async def test_fires_below_the_floor(self) -> None:
        result = await run_row(self.row, "One sentence only.")
        assert result.fired

    async def test_passes_inside_the_band(self) -> None:
        draft = "Models scale. Costs grow. Gains shrink."
        result = await run_row(self.row, draft)
        assert not result.fired


class TestBlockLength:
    row = BlockLength(name="tweet", rule="260 characters.", character_ceiling=260)

    async def test_fires_on_a_long_block(self) -> None:
        result = await run_row(self.row, "word " * 100)
        assert result.fired

    async def test_passes_on_a_short_block(self) -> None:
        result = await run_row(self.row, "A short, self-contained point.")
        assert not result.fired


class TestDraftWords:
    async def test_fires_under_the_floor(self) -> None:
        row = DraftWords(name="length", rule="150 words.", floor=150)
        result = await run_row(row, "Too short.")
        assert result.fired

    async def test_fires_over_the_ceiling(self) -> None:
        row = DraftWords(name="length", rule="Five words.", ceiling=5)
        result = await run_row(row, "One two three four five six seven.")
        assert result.fired

    async def test_passes_inside_the_band(self) -> None:
        row = DraftWords(name="length", rule="A range.", floor=1, ceiling=1000)
        result = await run_row(row, "A draft of a reasonable length.")
        assert not result.fired

    async def test_unbounded_never_fires(self) -> None:
        row = DraftWords(name="length", rule="No target.")
        result = await run_row(row, "Anything at all.")
        assert not result.fired


class TestSectionLength:
    row = SectionLength(name="sections", rule="800 words.", word_ceiling=10)

    async def test_fires_on_a_long_section(self) -> None:
        draft = "## Heading\n\n" + "word " * 40
        result = await run_row(self.row, draft)
        assert result.fired
        assert "Heading" in result.findings[0]

    async def test_passes_on_a_short_section(self) -> None:
        draft = "## Heading\n\nA short section."
        result = await run_row(self.row, draft)
        assert not result.fired


class TestJudgedRow:
    row = JudgedRow(
        name="teachable concreteness",
        rule="Claims arrive with something to check them against.",
        question="Is every claim concrete enough to teach from?",
    )

    async def test_reports_the_reviewer_verdict(self) -> None:
        judge = StubJudge(
            Verdict(
                holds=False,
                measured="2 of 5 claims carry no instance",
                findings=["Attention is expensive."],
            )
        )
        result = await run_row(self.row, "Attention is expensive.", judge)
        assert result.fired
        assert result.findings == ["Attention is expensive."]
        assert result.measured == "2 of 5 claims carry no instance"

    async def test_lands_in_the_same_row_shape(self) -> None:
        """A judged verdict is a CheckRow, so one report carries both tiers."""
        mechanical = await run_row(
            BannedVocabulary(name="banned", rule="No filler.", phrases=["crucial"]),
            "This is crucial.",
        )
        judged = await run_row(self.row, "Attention is expensive.")
        assert type(mechanical) is type(judged) is CheckRow

    async def test_asks_the_declared_question(self) -> None:
        judge = StubJudge(HELD)
        await run_row(self.row, "A draft.", judge)
        assert judge.asked == [self.row.question]


FOUNDATIONS = ChapterEntry(key="foundations", ordinal=1, title="Foundations")
"""Chapter one: laid out, numbered, and written, so it is expected to resolve."""

MEASUREMENT = ChapterEntry(key="measurement", ordinal=2, title="Measurement")
"""The chapter under check — the one whose draft the row is measured off."""

CONSEQUENCES = ChapterEntry(key="consequences", ordinal=3, title="Consequences")
"""Laid out and numbered, but nobody has written it: the forward reference."""

DROPPED = ChapterEntry(key="dropped", ordinal=4, title="Dropped")
"""Written, then dropped from the reading order, holding its ordinal."""

WROTE_FOUNDATIONS = ChapterRecord(
    placement=ChapterPlacement(book="textbook", chapter=1),
    title="Foundations",
    sections=["1.1 The calibration curve", "1.2 The error budget"],
)
"""What chapter one recorded — the sections a reference into it may name."""

WROTE_MEASUREMENT = ChapterRecord(
    placement=ChapterPlacement(book="textbook", chapter=2),
    title="Measurement",
    sections=["2.1 What we measure", "2.2 How we measure it"],
)
"""What the chapter under check recorded, which places its own passages."""

WROTE_DROPPED = ChapterRecord(
    placement=ChapterPlacement(book="textbook", chapter=4), title="Dropped"
)
"""A chapter that was written before the reading order let it go."""


def book(
    *,
    written: Sequence[ChapterRecord] = (WROTE_FOUNDATIONS, WROTE_MEASUREMENT),
    chapters: Sequence[ChapterEntry] = (FOUNDATIONS, MEASUREMENT, CONSEQUENCES),
    retired: Sequence[ChapterEntry] = (),
) -> BookRecord:
    """A book laid out in three chapters, with what it has written on file."""
    return BookRecord(
        book="textbook",
        outline=BookOutline(
            book="textbook", chapters=list(chapters), retired=list(retired)
        ),
        chapters=list(written),
    )


def dropping_a_written_chapter(
    written: Sequence[ChapterRecord] = (
        WROTE_FOUNDATIONS,
        WROTE_MEASUREMENT,
        WROTE_DROPPED,
    ),
) -> BookRecord:
    """The book after a layout that drops a written chapter from the order.

    Laid out through `relaid` rather than assembled by hand, because the whole
    question the book scope turns on is whether the state it reads is one the
    book stage can actually produce. `relaid` is the only writer of an
    outline, and it is what moves a dropped chapter into `retired` — while
    discarding, into a log, every cross-reference that named it.
    """
    before = BookRecord(
        book="textbook",
        outline=BookOutline(
            book="textbook",
            chapters=[FOUNDATIONS, MEASUREMENT, CONSEQUENCES, DROPPED],
            cross_references=[
                CrossReference(
                    from_chapter=1,
                    to_chapter=4,
                    relation="depends_on",
                    subject="the error budget",
                )
            ],
        ),
        chapters=list(written),
    )
    relaid = before.relaid(
        BookLayout(
            chapters=[
                ProposedChapter(key=held.key, title=held.title)
                for held in (FOUNDATIONS, MEASUREMENT, CONSEQUENCES)
            ]
        )
    )
    return before.model_copy(update={"outline": relaid})


def book_row(record: BookRecord, listed_ceiling: int = 20) -> UnresolvedReferences:
    """The row as a run writing chapter two of that book declares it."""
    return UnresolvedReferences(
        record=record,
        placement=ChapterPlacement(book="textbook", chapter=2),
        listed_ceiling=listed_ceiling,
    )


PLACED = "## 2.1 What we measure\n\n"
"""A heading the chapter's own record places, so a finding carries a section."""


class TestUnresolvedReferences:
    async def test_fires_on_a_reference_a_written_chapter_did_not_place(self) -> None:
        """The target chapter is written, so the reference was expected to land."""
        draft = (
            f"{PLACED}We lean on [the budget](book:foundations/the-budget) "
            f"throughout this section."
        )
        result = await run_row(book_row(book()), draft)
        assert result.fired
        assert len(result.findings) == 1

    async def test_a_book_where_everything_resolves_does_not_fire(self) -> None:
        """Every reference names a section its target chapter actually records."""
        draft = (
            f"{PLACED}Established in [the error budget]"
            f"(book:foundations/the-error-budget), and in "
            f"[foundations](book:foundations) at large."
        )
        result = await run_row(book_row(book()), draft)
        assert not result.fired
        assert result.findings == []

    async def test_a_forward_reference_to_an_unwritten_chapter_does_not_fire(
        self,
    ) -> None:
        """Chapter three is numbered and nobody has written it — that is the plan."""
        draft = (
            f"{PLACED}We return to this in "
            f"[the consequences](book:consequences/the-cost)."
        )
        result = await run_row(book_row(book()), draft)
        assert not result.fired
        assert "1 forward reference(s)" in result.measured

    async def test_a_key_no_layout_declared_does_not_fire(self) -> None:
        """Until the book stage names a key, nothing has numbered its chapter."""
        draft = f"{PLACED}Coming later in [the appendix](book:appendix)."
        result = await run_row(book_row(book()), draft)
        assert not result.fired

    async def test_a_finding_names_the_public_path_and_the_passage(self) -> None:
        """Actionable by whoever publishes: which page, and what it says."""
        draft = (
            f"{PLACED}We lean on [the budget](book:foundations/the-budget) "
            f"throughout this section."
        )
        result = await run_row(book_row(book()), draft)
        assert result.findings == [
            "/chapters/02/01 — “the budget” points at "
            "book:foundations/the-budget, and foundations is written at "
            "/chapters/01 but records no section “the-budget”. "
            "In: We lean on the budget throughout this section."
        ]

    async def test_an_unplaced_passage_is_named_by_its_chapter(self) -> None:
        """Prose before any heading has no section page of its own."""
        draft = "We lean on [the budget](book:foundations/the-budget) already."
        result = await run_row(book_row(book()), draft)
        assert result.findings[0].startswith("/chapters/02 ")

    async def test_a_written_then_retired_target_is_a_finding(self) -> None:
        """It was written, so it was expected to resolve — and now it cannot.

        The reference ships into the delivered document as a marker, which is
        exactly the link this row exists to report.
        """
        draft = f"{PLACED}Settled back in [the dropped one](book:dropped)."
        result = await run_row(book_row(dropping_a_written_chapter()), draft)
        assert result.fired
        assert result.findings[0] == (
            "/chapters/02/01 — “the dropped one” points at book:dropped, and "
            "dropped is written at /chapters/04 but is no longer in the "
            "reading order. In: Settled back in the dropped one."
        )

    async def test_a_retired_chapter_nobody_wrote_is_not_a_finding(self) -> None:
        """Dropped before anyone wrote it: nothing was ever expected to resolve."""
        record = dropping_a_written_chapter(
            written=[WROTE_FOUNDATIONS, WROTE_MEASUREMENT]
        )
        draft = f"{PLACED}Settled back in [the dropped one](book:dropped)."
        result = await run_row(book_row(record), draft)
        assert not result.fired

    async def test_the_book_scope_fires_where_the_chapter_resolves(self) -> None:
        """A chapter that places everything it names still reports the book's own.

        The state is one `relaid` produced, so this exercises what a run can
        reach rather than an outline assembled to suit the assertion.
        """
        record = dropping_a_written_chapter()
        outline = record.outline
        assert outline is not None
        assert [held.key for held in outline.retired] == ["dropped"]
        assert outline.cross_references == []
        result = await run_row(book_row(record), f"{PLACED}Nothing points anywhere.")
        assert result.fired
        assert result.findings == [
            "/chapters/04 — “Dropped” is written and the reading order no "
            "longer holds it, so every reference to book:dropped breaks "
            "wherever it was written, in this chapter or in one already "
            "delivered"
        ]

    async def test_the_book_scope_spares_a_chapter_nobody_wrote(self) -> None:
        """A chapter retired before it was written left no page to break."""
        record = dropping_a_written_chapter(
            written=[WROTE_FOUNDATIONS, WROTE_MEASUREMENT]
        )
        result = await run_row(book_row(record), f"{PLACED}Nothing points anywhere.")
        assert not result.fired

    async def test_both_scopes_are_reported_even_at_zero(self) -> None:
        """The measured line says what each scope found, so silence is legible."""
        result = await run_row(book_row(book()), f"{PLACED}Nothing points anywhere.")
        assert "this chapter:" in result.measured
        assert "this book:" in result.measured

    async def test_a_bounded_list_says_what_it_left_out(self) -> None:
        """A list cut short would otherwise read as the rest resolving."""
        pointing = " ".join(
            f"[budget {n}](book:foundations/the-budget-{n})." for n in range(5)
        )
        result = await run_row(book_row(book(), listed_ceiling=2), PLACED + pointing)
        assert len(result.findings) == 3
        assert "3 more not listed" in result.findings[-1]
        assert "5 broken reference(s) in all" in result.findings[-1]

    async def test_the_rule_states_it_reports_a_link_fact(self) -> None:
        """Not a voice judgement, and claiming no authority to stop anything."""
        rendered = render_declared_rules([book_row(book())])
        assert "link fact" in rendered
        assert "stops nothing" in rendered

    async def test_a_fired_row_reports_under_the_advisory_preamble(self) -> None:
        """Nothing a caller could read as a refusal — a line in a report."""
        draft = f"{PLACED}Leaning on [the budget](book:foundations/the-budget)."
        report = await run_format_checks(
            draft,
            [book_row(book())],
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.fired
        assert ADVISORY_PREAMBLE in report.render()

    async def test_a_failure_to_measure_is_dropped_rather_than_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One row that cannot be read must not take the whole report down."""

        def broken(markdown: str, references: BookReferences) -> NoReturn:
            raise RuntimeError("the walk fell over")

        monkeypatch.setattr(format_checks, "markdown_to_requests", broken)
        report = await run_format_checks(
            f"{PLACED}Leaning on [the budget](book:foundations/the-budget).",
            [book_row(book())],
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert not report.rows[0].fired
        assert "could not be read" in report.rows[0].measured


class TestDeclaringTheRow:
    """Where the row comes from: the book on the plan, not the format."""

    @pytest.fixture
    def notes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PipelineNotes:
        """A run's notes, with the book store pointed at the same temp root."""
        monkeypatch.setattr(config_mod.settings, "books_path", str(tmp_path / "books"))
        return PipelineNotes(tmp_path / "run")

    def plan(self, placement: ChapterPlacement | None) -> ArticlePlan:
        """A plan that either is a chapter of a book or is standalone."""
        return ArticlePlan(
            title="Measurement",
            thesis="What we measure decides what we see",
            target_format="blog",
            placement=placement,
            sections=[
                SectionPlan(title="2.1 What we measure", summary="", key_points=[])
            ],
            research_questions=[],
            source_quotes=[],
            author_direction="",
            voice_notes="",
        )

    def test_a_chapter_run_declares_the_row(self, notes: PipelineNotes) -> None:
        """The plan carries a book identity, so references can go unresolved."""
        notes.save_artifact(
            "plan", self.plan(ChapterPlacement(book="textbook", chapter=2))
        )
        assert [c.name for c in declared_format_checks(notes)] == ["book references"]

    def test_a_standalone_piece_declares_nothing(self, notes: PipelineNotes) -> None:
        """A bookless piece has no chapters to point at, so no row to be silent."""
        notes.save_artifact("plan", self.plan(None))
        assert declared_format_checks(notes) == []

    def test_the_row_travels_in_any_format(self, notes: PipelineNotes) -> None:
        """The row follows the book identity, so no format declaration names it."""
        notes.save_artifact(
            "plan", self.plan(ChapterPlacement(book="textbook", chapter=2))
        )
        declared = declared_format_checks(notes)
        for target_format in ("twitter", "memo", "textbook"):
            assert "book references" in [
                c.name for c in format_checks_for(target_format, declared)
            ]

    def test_the_row_survives_a_resume(self, notes: PipelineNotes) -> None:
        """Read off the artifacts, so a stage not in this process still declares it.

        The rewriter reads what the planner wrote from disk and nowhere else,
        which is what a resumed run has: files, and no earlier stage to ask.
        """
        notes.save_artifact(
            "plan", self.plan(ChapterPlacement(book="textbook", chapter=2))
        )
        resumed = PipelineNotes(notes.base_dir)
        assert "link fact" in get_format_guidance(
            "twitter", declared_format_checks(resumed)
        )


class TestDeclaredRulesRendering:
    def test_renders_each_rule_for_the_writer(self) -> None:
        rendered = render_declared_rules(
            [BannedVocabulary(name="banned", rule="No filler phrases.", phrases=["x"])]
        )
        assert "banned" in rendered
        assert "No filler phrases." in rendered

    def test_no_rows_renders_nothing(self) -> None:
        assert render_declared_rules([]) == ""
