# claude: ignore
"""Tests for the checks a format declares beside its guidance."""

from pathlib import Path

from inkwell.agent.format_checks import (
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
    TerminologyDrift,
    Verdict,
    render_declared_rules,
    run_format_checks,
)
from inkwell.agent.book import BookStore, ChapterPlacement
from inkwell.agent.glossary import (
    BookGlossary,
    ChapterGlossary,
    GlossaryEntry,
    GlossaryScope,
    RunGlossary,
    write_chapter_glossary,
)
from inkwell.agent.segmenter import reader


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
    check: FormatCheck,
    draft: str,
    judge: Judge | None = None,
    glossary: GlossaryScope | None = None,
) -> CheckRow:
    """One declared row's verdict on one draft."""
    report = await run_format_checks(
        draft,
        [check],
        format_key="test",
        draft_path=Path("draft.md"),
        judge=judge or StubJudge(HELD),
        reader=reader(),
        glossary=glossary,
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


class TestTerminologyDrift:
    """A name the book settled, and a draft reaching for one it rejected."""

    row = TerminologyDrift(
        name="terminology drift", rule="Call each thing what the glossary calls it."
    )

    def book(self, root: Path, *entries: GlossaryEntry) -> BookGlossary:
        """Chapter two of a book whose first chapter settled these names."""
        store = BookStore(root=root)
        first = BookGlossary(
            store=store, placement=ChapterPlacement(book="textbook", chapter=1)
        )
        write_chapter_glossary(first.own(), ChapterGlossary(terms=list(entries)))
        return BookGlossary(
            store=store, placement=ChapterPlacement(book="textbook", chapter=2)
        )

    SETTLED = GlossaryEntry(
        term="inner alignment",
        meaning="the mesa-objective gap",
        aliases=["goal misgeneralization"],
    )

    async def test_fires_naming_the_term_the_draft_should_have_used(
        self, tmp_path: Path
    ) -> None:
        result = await run_row(
            self.row,
            "This chapter returns to goal misgeneralization.",
            glossary=self.book(tmp_path, self.SETTLED),
        )
        assert result.fired
        assert "inner alignment" in result.findings[0]
        assert "goal misgeneralization" in result.findings[0]

    async def test_holds_when_the_draft_keeps_the_settled_name(
        self, tmp_path: Path
    ) -> None:
        result = await run_row(
            self.row,
            "This chapter returns to inner alignment.",
            glossary=self.book(tmp_path, self.SETTLED),
        )
        assert not result.fired

    async def test_matches_words_rather_than_substrings(self, tmp_path: Path) -> None:
        """A rejected name inside a longer word is not that name."""
        result = await run_row(
            self.row,
            "The chapter is about goals, misgeneralizing from one case.",
            glossary=self.book(tmp_path, self.SETTLED),
        )
        assert not result.fired

    async def test_an_undeclared_synonym_is_invisible_to_it(
        self, tmp_path: Path
    ) -> None:
        """What the mechanical row cannot see, and what its rule has to say."""
        result = await run_row(
            self.row,
            "This chapter returns to objective misgeneralisation.",
            glossary=self.book(tmp_path, self.SETTLED),
        )
        assert not result.fired

    async def test_an_entry_listing_its_own_term_rejected_nothing(
        self, tmp_path: Path
    ) -> None:
        """Or the canonical name would be reported as a rename of itself."""
        echoed = GlossaryEntry(
            term="inner alignment",
            meaning="the mesa-objective gap",
            aliases=["Inner Alignment"],
        )
        result = await run_row(
            self.row,
            "This chapter returns to inner alignment.",
            glossary=self.book(tmp_path, echoed),
        )
        assert not result.fired
        assert "0 rival name(s)" in result.measured

    async def test_a_book_with_no_terms_yet_counts_none(self, tmp_path: Path) -> None:
        result = await run_row(
            self.row,
            "A first chapter, naming nothing yet.",
            glossary=self.book(tmp_path),
        )
        assert not result.fired
        assert "0 term(s) on file" in result.measured

    async def test_measures_nothing_without_a_book(self, tmp_path: Path) -> None:
        """A piece in no book is not quietly reported as consistent."""
        result = await run_row(
            self.row,
            "A standalone article that names things.",
            glossary=RunGlossary(path=tmp_path / "glossary.json"),
        )
        assert not result.fired
        assert "nothing was measured" in result.measured

    async def test_measures_nothing_when_no_glossary_is_composed(self) -> None:
        result = await run_row(self.row, "A draft checked with no glossary to hand.")
        assert not result.fired
        assert "nothing was measured" in result.measured


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


class TestDeclaredRulesRendering:
    def test_renders_each_rule_for_the_writer(self) -> None:
        rendered = render_declared_rules(
            [BannedVocabulary(name="banned", rule="No filler phrases.", phrases=["x"])]
        )
        assert "banned" in rendered
        assert "No filler phrases." in rendered

    def test_no_rows_renders_nothing(self) -> None:
        assert render_declared_rules([]) == ""
