# claude: ignore
"""Tests for the checks a format declares beside its guidance."""

from pathlib import Path

from inkwell.agent.format_checks import (
    BannedVocabulary,
    BlockLength,
    BoldedSummaries,
    BoldEmphasis,
    CheckRow,
    ContractionRate,
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
    QuestionRate,
    RuleOfThree,
    SectionLength,
    SentenceLengthVariance,
    SentenceOpeners,
    ShortSentences,
    SynonymCycling,
    SynonymSet,
    VagueAttribution,
    Verdict,
    render_declared_rules,
    run_format_checks,
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


class TestSynonymCycling:
    row = SynonymCycling(name="cycling", rule="Say the word again.")

    async def test_fires_when_a_paragraph_renames_one_thing(self) -> None:
        draft = (
            "The researcher examined the data. The scientist then consulted "
            "with colleagues. The academic published her findings."
        )
        result = await run_row(self.row, draft)
        assert result.fired
        assert result.findings

    async def test_passes_when_the_word_is_repeated(self) -> None:
        """What the guidance actually asks for must never read as the tell."""
        draft = (
            "The researcher examined the data. The researcher consulted "
            "colleagues. The researcher published her findings."
        )
        result = await run_row(self.row, draft)
        assert not result.fired

    async def test_a_plural_is_the_same_term(self) -> None:
        draft = "The researcher led it. The researchers agreed. The researcher won."
        assert not (await run_row(self.row, draft)).fired

    async def test_two_terms_is_variation_and_not_a_cycle(self) -> None:
        draft = "The researcher examined the data. The scientist agreed with her."
        assert not (await run_row(self.row, draft)).fired

    async def test_the_families_are_overridable(self) -> None:
        row = self.row.model_copy(
            update={
                "families": [SynonymSet(terms=[["carrot"], ["root"], ["vegetable"]])]
            }
        )
        assert not (
            await run_row(row, "The scientist met the academic and expert.")
        ).fired
        assert (
            await run_row(row, "The carrot is a root, and the vegetable sells.")
        ).fired

    async def test_cycling_across_paragraphs_is_not_the_tell(self) -> None:
        """Renaming is what a reader notices inside one paragraph."""
        draft = "The researcher won.\n\nThe scientist agreed.\n\nThe academic left."
        assert not (await run_row(self.row, draft)).fired


class TestRuleOfThree:
    row = RuleOfThree(name="threes", rule="Count first.", share_ceiling=0.0)

    async def test_fires_on_three_adjectives(self) -> None:
        result = await run_row(
            self.row, "The program was innovative, transformative, and groundbreaking."
        )
        assert result.fired
        assert result.findings

    async def test_fires_on_three_short_phrases(self) -> None:
        result = await run_row(self.row, "Models scale, costs grow, and gains shrink.")
        assert result.fired

    async def test_passes_on_a_pair(self) -> None:
        result = await run_row(self.row, "The program was innovative and useful.")
        assert not result.fired

    async def test_passes_on_a_list_of_four(self) -> None:
        """The tell is three, so four is the content deciding the number."""
        result = await run_row(self.row, "It runs on Linux, macOS, Windows, and BSD.")
        assert not result.fired

    async def test_two_coordinated_clauses_are_not_a_list(self) -> None:
        result = await run_row(
            self.row, "The model failed the benchmark, and the baseline won outright."
        )
        assert not result.fired

    async def test_a_triple_the_content_fixed_at_three_does_not_fire(self) -> None:
        """A default above zero, so one genuine triple is not a finding.

        A neuron really does have three parts. The row is about the habit of
        reaching for three, which only a draft doing it repeatedly shows.
        """
        row = RuleOfThree(name="threes", rule="Count first.")
        draft = (
            "A neuron has a body, an axon, and dendrites. That is anatomy, not "
            "rhetoric. Every other sentence here makes one point. Models scale "
            "with data. Costs grow with depth. Gains shrink at the margin. The "
            "trend has held for a decade. Nothing about it is guaranteed to "
            "continue. Measure it again next year. Then decide what to build. "
            "The anatomy is worth knowing first. A body holds the machinery. "
            "An axon carries the signal out. Dendrites take the signal in. "
            "None of that is a rhetorical choice."
        )
        result = await run_row(row, draft)
        assert not result.fired, "one true triple in a draft is not the habit"

    async def test_the_item_cap_is_overridable(self) -> None:
        row = self.row.model_copy(update={"item_words": 4})
        draft = (
            "The model failed the benchmark, the baseline won outright, and "
            "the gap held firm."
        )
        assert not (await run_row(self.row, draft)).fired
        assert (await run_row(row, draft)).fired


class TestVagueAttribution:
    row = VagueAttribution(name="attribution", rule="Name who said it.")

    async def test_fires_on_an_unnamed_appeal(self) -> None:
        result = await run_row(self.row, "Studies show that the effect persists.")
        assert result.fired
        assert result.findings

    async def test_passes_when_the_sentence_names_a_source(self) -> None:
        result = await run_row(
            self.row, "Researchers at DeepMind found that the effect persists."
        )
        assert not result.fired

    async def test_passes_on_prose_with_no_appeal(self) -> None:
        result = await run_row(self.row, "The effect persists across every run.")
        assert not result.fired

    async def test_the_blessed_hedge_is_not_an_appeal(self) -> None:
        """The guidance lists `the evidence suggests` among the good moves."""
        result = await run_row(self.row, "The evidence suggests the effect persists.")
        assert not result.fired

    async def test_the_ceiling_is_overridable(self) -> None:
        tolerant = self.row.model_copy(update={"ceiling": 1})
        result = await run_row(tolerant, "Studies show that the effect persists.")
        assert not result.fired


class TestMeasurementRows:
    """Rows that report a number and reach no verdict, for what the guidance
    asks for rather than against."""

    async def test_contractions_are_counted_when_present(self) -> None:
        row = ContractionRate(name="contractions", rule="Contract.")
        result = await run_row(row, "It's the cost that grows. Don't ignore it.")
        assert "2/2" in result.measured
        assert result.measurement
        assert not result.fired

    async def test_contractions_read_zero_on_formal_prose(self) -> None:
        row = ContractionRate(name="contractions", rule="Contract.")
        result = await run_row(row, "It is the cost that grows. Do not ignore it.")
        assert "0/2" in result.measured
        assert not result.fired

    async def test_a_possessive_is_not_a_contraction(self) -> None:
        row = ContractionRate(name="contractions", rule="Contract.")
        result = await run_row(row, "The model's weights are frozen.")
        assert "0/1" in result.measured

    async def test_conjunction_openings_are_counted(self) -> None:
        row = SentenceOpeners(name="openings", rule="And is fine.")
        assert (
            "1/2"
            in (
                await run_row(row, "The cost grows. But the cache absorbs it.")
            ).measured
        )
        assert (
            "0/2"
            in (await run_row(row, "The cost grows. The cache absorbs it.")).measured
        )

    async def test_questions_are_counted(self) -> None:
        row = QuestionRate(name="questions", rule="Ask.")
        assert (
            "1/2"
            in (
                await run_row(row, "Why does it scale? The cache absorbs the cost.")
            ).measured
        )
        assert "0/1" in (await run_row(row, "The cache absorbs the cost.")).measured

    async def test_short_sentences_are_counted(self) -> None:
        row = ShortSentences(name="short", rule="Fragments welcome.", word_ceiling=3)
        assert (
            "1/2"
            in (
                await run_row(row, "It broke. The whole evaluation suite came apart.")
            ).measured
        )
        assert (
            "0/1"
            in (await run_row(row, "The whole evaluation suite came apart.")).measured
        )

    async def test_no_measurement_row_ever_fires(self) -> None:
        """A measurement holds nothing against a draft, whatever it counts."""
        rows: list[FormatCheck] = [
            ContractionRate(name="contractions", rule="Contract."),
            SentenceOpeners(name="openings", rule="And is fine."),
            QuestionRate(name="questions", rule="Ask."),
            ShortSentences(name="short", rule="Fragments welcome."),
            PunctuationDensity(name="semicolons", rule="Use them.", marks=[";"]),
        ]
        formal = "It is the case that the cost of the whole evaluation suite grows."
        casual = "Don't. But why? It broke; badly."
        for row in rows:
            for draft in (formal, casual):
                result = await run_row(row, draft)
                assert not result.fired
                assert result.measurement
                assert result.state == "measured"


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


class TestDeclaredRulesRendering:
    def test_renders_each_rule_for_the_writer(self) -> None:
        rendered = render_declared_rules(
            [BannedVocabulary(name="banned", rule="No filler phrases.", phrases=["x"])]
        )
        assert "banned" in rendered
        assert "No filler phrases." in rendered

    def test_no_rows_renders_nothing(self) -> None:
        assert render_declared_rules([]) == ""


class TestReportRendering:
    async def test_a_measurement_prints_a_number_and_no_verdict(self) -> None:
        """`ok` would claim a judgement the row never reached."""
        report = await run_format_checks(
            "The cost grows. But it's bounded.",
            [
                ContractionRate(name="contractions", rule="Contract."),
                BannedVocabulary(name="banned", rule="No filler.", phrases=["myriad"]),
            ],
            format_key="test",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        rendered = report.render()
        assert "contractions: measured — 1/2 sentence(s) contract (50%)" in rendered
        assert "banned: ok —" in rendered
        assert "0/1 rows fired (advisory), 1 measured" in rendered

    async def test_the_preamble_explains_what_a_measurement_is(self) -> None:
        report = await run_format_checks(
            "A draft.",
            [ContractionRate(name="contractions", rule="Contract.")],
            format_key="test",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert "reached no verdict" in report.render()
