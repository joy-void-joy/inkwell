# claude: ignore
"""Tests for what a format declares, and where those declarations reach."""

from pathlib import Path

import pytest

from inkwell.agent.content import ContentManifest
from inkwell.agent.format_checks import (
    ADVISORY_PREAMBLE,
    BannedVocabulary,
    BoldedSummaries,
    BoldEmphasis,
    JudgedRow,
    LinkingPredicates,
    PunctuationDensity,
    TerminologyDrift,
    run_format_checks,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import add_format_check_report, format_checks_path
from inkwell.agent.segmenter import reader
from inkwell.agent.stages import (
    EVERY_FORMAT_CHECKS,
    OUTPUT_FORMATS,
    FORMAT_KEYS,
    format_checks_for,
    get_format_guidance,
)
from inkwell.agent.tools.stage_outputs import (
    DeclareFormatCheckInput,
    FormatCheckCollector,
    load_declared_checks,
    make_format_check_tools,
)
from inkwell.agent.voice_tells import (
    BANNED_VOCABULARY,
    BANNED_VOCABULARY_PROSE,
    LLM_VOCABULARY,
    VOICE_TELL_CHECKS,
)
from tests.unit.test_format_checks import HELD, StubJudge

TEMPLATED = (
    "**Attention is expensive.** It's worth noting that the cost is quadratic.\n\n"
    "**Caching is helpful.** Moreover, the keys are reusable.\n\n"
    "**Batching is useful.** Moreover, the tokens are shareable."
)
"""A draft built to trip the textbook rows: filler vocabulary, repeated
openings, uniform rhythm, and copular predicates throughout."""


class TestTextbookFormat:
    spec = next(f for f in OUTPUT_FORMATS if f.key == "textbook")

    def test_is_declared(self) -> None:
        assert self.spec.label
        assert self.spec.guidance

    def test_carries_the_bolded_summary_convention(self) -> None:
        assert "bolded sentence" in self.spec.guidance
        assert "follow the bold" in self.spec.guidance.lower() or (
            "only the bolded" in self.spec.guidance
        )

    def test_carries_the_enumerated_tells(self) -> None:
        for heading in (
            "Vocabulary to avoid",
            "Structural tells",
            "Tone",
            "Punctuation",
        ):
            assert heading in self.spec.guidance

    def test_carries_the_punctuation_and_artifact_guidance(self) -> None:
        assert "Em dashes are rationed" in self.spec.guidance
        assert "No participial tails" in self.spec.guidance
        assert "No markdown left showing" in self.spec.guidance

    def test_the_voice_document_wins_on_semicolons_and_parentheses(self) -> None:
        """Both documents are normative and they disagreed; the voice one won.

        The voice guidance argues that machine prose *avoids* these marks and
        tells the writer to use them. This format used to ration both. Where
        the two disagree the voice guidance is the one that stands, so the
        prose stopped rationing them and the rows stopped judging them.
        """
        assert "Semicolons are rationed" not in self.spec.guidance
        assert "Parentheses are rationed" not in self.spec.guidance
        assert "Semicolons and parentheses are normal punctuation" in (
            self.spec.guidance
        )

        marks = {
            check.name: check
            for check in self.spec.checks
            if isinstance(check, PunctuationDensity)
        }
        assert marks["semicolon density"].per_thousand_ceiling is None
        assert marks["parenthesis density"].per_thousand_ceiling is None
        assert marks["em-dash density"].per_thousand_ceiling is not None

    async def test_a_semicolon_heavy_draft_is_measured_and_not_faulted(self) -> None:
        """The rows and the prose agree afterwards: a count, and no verdict."""
        row = next(
            check
            for check in self.spec.checks
            if isinstance(check, PunctuationDensity)
            and check.name == "semicolon density"
        )
        report = await run_format_checks(
            "Models scale; costs grow; gains shrink; the trend holds.",
            [row],
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.fired == []
        assert report.measurements == report.rows
        assert "3 of ;" in report.rows[0].measured

    def test_resolves_the_bold_collision_in_prose(self) -> None:
        """Bold is banned as emphasis and mandated as navigation; say which."""
        assert "Bold, in this format only" in self.spec.guidance
        assert "do not bold anything inside the body" in self.spec.guidance

    def test_declares_rows_in_both_tiers(self) -> None:
        kinds = {check.kind for check in self.spec.checks}
        assert "judged" in kinds
        assert kinds - {"judged"}

    def test_names_the_teach_from_judged_row(self) -> None:
        judged = [c for c in self.spec.checks if isinstance(c, JudgedRow)]
        assert any("teach from" in c.rule or "teach from" in c.question for c in judged)

    def test_reaches_the_cli_format_keys(self) -> None:
        """The CLI's help is derived from the format roster, not restated.

        Asserted on the declaration the command file is generated from: a
        format added to OUTPUT_FORMATS reaches `--format` by being in the
        roster, so there is no second list here that could fall behind.
        """
        from inkwell.environment.entrypoints import TARGET_FORMAT

        assert "textbook" in TARGET_FORMAT.help
        assert "textbook" in FORMAT_KEYS

    async def test_reaches_the_formats_endpoint(self) -> None:
        from inkwell.environment.web.routes.sessions import get_formats

        assert "textbook" in {option.key for option in await get_formats()}

    def test_reaches_the_stage_prompts(self) -> None:
        """The plan, write, and rewrite stages all read this one block."""
        guidance = get_format_guidance("textbook")
        assert "Format: Textbook" in guidance
        assert "Rules this format checks" in guidance
        assert "voice outranks" in guidance.lower()

    def test_every_declared_row_states_its_rule_to_the_writer(self) -> None:
        guidance = get_format_guidance("textbook")
        for check in self.spec.checks:
            assert check.name in guidance
            assert check.rule in guidance


class TestVoiceTellCoverage:
    """The rows cover the voice guidance's breadth, not a sample of it."""

    spec = next(f for f in OUTPUT_FORMATS if f.key == "textbook")
    row = next(
        check
        for check in spec.checks
        if isinstance(check, BannedVocabulary) and check.name == "llm vocabulary"
    )

    def test_the_row_measures_every_group_the_guidance_names(self) -> None:
        assert [group.label for group in BANNED_VOCABULARY] == [
            "Nouns",
            "Verbs",
            "Adjectives",
            "Adverbs",
            "Phrases",
        ]
        watched = dict.fromkeys(self.row.phrases)
        for group in BANNED_VOCABULARY:
            assert all(word in watched for word in group.words), group.label

    def test_the_list_is_the_guidance_and_not_a_sample_of_it(self) -> None:
        """It held 32 entries against a section banning some two hundred."""
        assert len(self.row.phrases) > 200

    @pytest.mark.parametrize(
        "draft",
        [
            "The tapestry of results holds.",
            "We leverage the corpus to get there.",
            "The finding is groundbreaking.",
            "Crucially, the effect held.",
            "It's worth noting that the effect held.",
        ],
        ids=["noun", "verb", "adjective", "adverb", "phrase"],
    )
    async def test_a_word_from_each_group_reaches_the_row(self, draft: str) -> None:
        report = await run_format_checks(
            draft,
            [self.row],
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.fired

    def test_the_vocabulary_stays_an_overridable_default(self) -> None:
        """A house with different tells declares its own rather than forking."""
        house = self.row.model_copy(update={"phrases": ["synergy"]})
        assert house.phrases == ["synergy"]
        assert self.row.phrases == LLM_VOCABULARY

    async def test_a_house_list_replaces_the_default_wholesale(self) -> None:
        house = self.row.model_copy(update={"phrases": ["synergy"]})
        report = await run_format_checks(
            "The tapestry of results holds real synergy.",
            [house],
            format_key="house",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.rows[0].findings == [
            "“synergy” — The tapestry of results holds real synergy."
        ]

    def test_the_guidance_prose_is_rendered_from_the_row_s_own_data(self) -> None:
        """One list with two readers, so the two cannot drift apart."""
        assert BANNED_VOCABULARY_PROSE in self.spec.guidance
        for phrase in self.row.phrases:
            assert phrase in self.spec.guidance, phrase

    @pytest.mark.parametrize(
        "name",
        ["synonym cycling", "rule of three", "vague attribution"],
    )
    def test_the_missing_tells_got_a_row(self, name: str) -> None:
        assert any(check.name == name for check in self.spec.checks)

    def test_the_copula_row_covers_the_wider_inflation_family(self) -> None:
        """Both families the guidance names: `is` inflated, and a plain verb."""
        row = next(
            check for check in self.spec.checks if isinstance(check, LinkingPredicates)
        )
        inflated = ("serves as", "boasts", "utilize", "facilitate", "leverage")
        assert all(phrase in row.phrases for phrase in inflated)

    async def test_an_inflated_plain_verb_fires_the_copula_row(self) -> None:
        row = next(
            check for check in self.spec.checks if isinstance(check, LinkingPredicates)
        )
        report = await run_format_checks(
            "The team utilized the corpus to facilitate the comparison.",
            [row],
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.fired

    def test_every_new_row_states_a_threshold_as_a_field(self) -> None:
        """A format tunes a row without editing its class."""
        tunable = {
            "synonym cycling": "distinct_ceiling",
            "rule of three": "share_ceiling",
            "vague attribution": "ceiling",
            "short sentences": "word_ceiling",
        }
        declared = {check.name: check for check in VOICE_TELL_CHECKS}
        for name, field in tunable.items():
            assert field in type(declared[name]).model_fields
            assert getattr(declared[name], field) is not None

    async def test_every_voice_row_travels_under_the_preamble(self) -> None:
        report = await run_format_checks(
            TEXTBOOK_PROSE,
            VOICE_TELL_CHECKS,
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        rendered = report.render()
        assert ADVISORY_PREAMBLE in rendered
        for check in VOICE_TELL_CHECKS:
            assert check.name in rendered

    def test_every_voice_row_states_its_rule_to_the_writer(self) -> None:
        guidance = get_format_guidance("textbook")
        for check in VOICE_TELL_CHECKS:
            assert check.rule in guidance, check.name


class TestSharedVoiceDeclaration:
    """One declaration a format draws on, rather than a textbook-only list."""

    def test_the_textbook_draws_on_it(self) -> None:
        declared = format_checks_for("textbook")
        assert declared[-len(VOICE_TELL_CHECKS) :] == VOICE_TELL_CHECKS

    def test_only_the_teaching_rows_are_the_textbook_s_own(self) -> None:
        own = format_checks_for("textbook")[
            len(EVERY_FORMAT_CHECKS) : -len(VOICE_TELL_CHECKS)
        ]
        assert [check.name for check in own] == [
            "bolded summaries",
            "bold emphasis",
            "teachable concreteness",
            "dependency order",
        ]

    @pytest.mark.parametrize(
        "key",
        ["academic", "lesswrong", "blog", "twitter", "dialog", "memo", "linkedin"],
    )
    def test_no_other_format_changed(self, key: str) -> None:
        """Available to them, and adopted by none of them in this pass."""
        voice = [check.name for check in VOICE_TELL_CHECKS]
        assert all(check.name not in voice for check in format_checks_for(key))


class TestNewRowKindsAreFirstClass:
    """Each new row is a kind, so a run can declare one over the tool."""

    @pytest.mark.parametrize(
        "kind",
        [
            "synonym-cycling",
            "rule-of-three",
            "vague-attribution",
            "contraction-rate",
            "sentence-openers",
            "question-rate",
            "short-sentences",
        ],
    )
    def test_the_kind_validates_through_the_declaration_tool(self, kind: str) -> None:
        declared = DeclareFormatCheckInput.model_validate(
            {"check": {"kind": kind, "name": "house row", "rule": "A house rule."}}
        )
        assert declared.check.kind == kind


class TestFormatsWithoutRows:
    def test_a_format_with_no_rows_stays_valid(self) -> None:
        newsletter = next(f for f in OUTPUT_FORMATS if f.key == "newsletter")
        assert newsletter.checks == []

    async def test_no_rows_reports_an_empty_report(self) -> None:
        report = await run_format_checks(
            "A draft.",
            [],
            format_key="newsletter",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.rows == []
        assert report.fired == []

    def test_unknown_format_declares_none_of_its_own(self) -> None:
        """A format nobody declared contributes no rows, and still carries the
        rows every format carries — terminology is not a format's convention."""
        assert format_checks_for("auto") == EVERY_FORMAT_CHECKS
        assert "Format:" not in get_format_guidance("auto")
        assert "terminology drift" in get_format_guidance("auto")


class TestRowsEveryFormatCarries:
    """Terminology is a fact about the piece, not a convention of one format."""

    row = next(c for c in EVERY_FORMAT_CHECKS if isinstance(c, TerminologyDrift))

    @pytest.mark.parametrize("key", [spec.key for spec in OUTPUT_FORMATS])
    def test_every_format_is_measured_by_it(self, key: str) -> None:
        assert self.row in format_checks_for(key)

    @pytest.mark.parametrize("key", ["textbook", "newsletter", "auto"])
    def test_the_writer_reads_the_rule_the_draft_is_measured_by(self, key: str) -> None:
        """Including a format that declares nothing else, which had no block."""
        assert self.row.rule in get_format_guidance(key)

    def test_the_rule_states_what_the_row_cannot_see(self) -> None:
        """A mechanical row's silence must not read as an all-clear."""
        assert "cannot see" in self.row.rule
        assert "synonym" in self.row.rule

    def test_it_is_mechanical(self) -> None:
        """No reviewer is spent per draft; a judged sibling would be its own row."""
        assert self.row.kind != "judged"


class TestExistingFormatsGotRows:
    @pytest.mark.parametrize(
        "key",
        ["academic", "lesswrong", "blog", "twitter", "dialog", "memo", "linkedin"],
    )
    def test_measurable_guidance_became_rows(self, key: str) -> None:
        spec = next(f for f in OUTPUT_FORMATS if f.key == key)
        assert spec.checks, f"{key} states a measurable rule but declares no row"

    def test_memo_bolding_rule_is_the_inverse_of_textbook(self) -> None:
        """One row kind states both conventions, as data rather than as code."""
        memo = next(f for f in OUTPUT_FORMATS if f.key == "memo")
        textbook = next(f for f in OUTPUT_FORMATS if f.key == "textbook")
        memo_bold = next(c for c in memo.checks if isinstance(c, BoldedSummaries))
        book_bold = next(c for c in textbook.checks if isinstance(c, BoldedSummaries))
        assert memo_bold.share_ceiling < book_bold.share_floor


TEXTBOOK_PROSE = (
    "**Attention costs quadratic time.** Doubling the context quadruples the "
    "compute.\n\n"
    "**Caching keys removes the repeat work.** Store what you already computed, "
    "then read it back on the next turn. For a chat transcript nearly all of "
    "the prompt repeats, so the saving compounds over a long conversation.\n\n"
    "**The window is a budget, not a limit.** A model with a large context can "
    "still be starved by a prompt that spends its tokens on boilerplate. Trim "
    "the system preamble first. Then measure again, because the number you care "
    "about is tokens the model actually reads, not tokens you were allowed to "
    "send."
)
"""A draft written the way the textbook format asks: every paragraph opens on a
bolded summary, nothing inside a paragraph is emphasized, and the sentence and
paragraph lengths vary. Every mechanical textbook row holds on it."""


class TestBoldCollisionPerFormat:
    """The author's resolution, declared per format rather than argued per row."""

    async def test_textbook_bold_row_passes_its_own_convention(self) -> None:
        row = next(
            c for c in format_checks_for("textbook") if isinstance(c, BoldEmphasis)
        )
        report = await run_format_checks(
            TEXTBOOK_PROSE,
            [row],
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert not report.fired, "the mandated bold must not read as overuse"

    async def test_memo_bold_row_fires_on_the_same_draft(self) -> None:
        """Memo grants no exemption, so the same prose trips its row."""
        row = next(c for c in format_checks_for("memo") if isinstance(c, BoldEmphasis))
        report = await run_format_checks(
            TEXTBOOK_PROSE,
            [row],
            format_key="memo",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.fired

    async def test_textbook_rows_pass_a_draft_written_to_them(self) -> None:
        """Every mechanical textbook row holds on prose that follows the format."""
        mechanical = [c for c in format_checks_for("textbook") if c.mechanical]
        report = await run_format_checks(
            TEXTBOOK_PROSE,
            mechanical,
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert [row.name for row in report.fired] == []


class TestAdvisoryNotGating:
    async def test_a_draft_firing_every_row_still_reports(self) -> None:
        mechanical = [c for c in format_checks_for("textbook") if c.mechanical]
        report = await run_format_checks(
            TEMPLATED,
            mechanical,
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert report.fired, "fixture was meant to trip rows"
        assert len(report.rows) == len(mechanical)
        assert "advisory" in report.render()

    async def test_the_report_carries_the_precedence(self) -> None:
        report = await run_format_checks(
            TEMPLATED,
            format_checks_for("twitter"),
            format_key="twitter",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )
        rendered = report.render()
        assert "do not gate" in rendered
        assert "voice" in rendered
        assert "outrank" in rendered

    async def test_the_rewrite_stage_reads_the_rows(self, tmp_path: Path) -> None:
        notes = PipelineNotes(tmp_path)
        draft_path = tmp_path / "draft.md"
        draft_path.write_text(TEMPLATED, encoding="utf-8")
        manifest = ContentManifest()
        await add_format_check_report(
            manifest,
            notes,
            TEMPLATED,
            draft_path,
            target_format="twitter",
            judge=StubJudge(HELD),
        )
        listed = [ref for ref in manifest.refs if ref.label == "Format checks"]
        assert listed, "the rewrite stage was not given the report"
        assert "advisory" in listed[0].instruction
        assert "advisory" in Path(listed[0].path).read_text(encoding="utf-8")

    async def test_a_broken_reader_does_not_stop_the_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not even failing to measure can gate: it reports nothing and goes on."""

        def explode() -> None:
            raise RuntimeError("no language model")

        monkeypatch.setattr("inkwell.agent.pipeline.reader", explode)
        manifest = ContentManifest()
        await add_format_check_report(
            manifest,
            PipelineNotes(tmp_path),
            TEMPLATED,
            tmp_path / "draft.md",
            target_format="twitter",
        )
        assert manifest.refs == []


class TestCustomFormatTool:
    async def test_declares_a_row_at_runtime(self, tmp_path: Path) -> None:
        notes = PipelineNotes(tmp_path)
        collector = FormatCheckCollector(format_checks_path(notes))
        tool = make_format_check_tools(collector)[0]

        await tool.call_handler(
            DeclareFormatCheckInput.model_validate(
                {
                    "check": {
                        "kind": "banned-vocabulary",
                        "name": "house style",
                        "rule": "Never say synergy.",
                        "phrases": ["synergy"],
                    }
                }
            )
        )

        declared = load_declared_checks(format_checks_path(notes))
        assert [check.name for check in declared] == ["house style"]

    async def test_a_tool_declared_row_runs_against_a_draft(
        self, tmp_path: Path
    ) -> None:
        notes = PipelineNotes(tmp_path)
        collector = FormatCheckCollector(format_checks_path(notes))
        tool = make_format_check_tools(collector)[0]
        await tool.call_handler(
            DeclareFormatCheckInput.model_validate(
                {
                    "check": {
                        "kind": "banned-vocabulary",
                        "name": "house style",
                        "rule": "Never say synergy.",
                        "phrases": ["synergy"],
                    }
                }
            )
        )

        declared = load_declared_checks(format_checks_path(notes))
        report = await run_format_checks(
            "The teams found real synergy.",
            format_checks_for("custom:a house memo", declared),
            format_key="custom",
            draft_path=tmp_path / "draft.md",
            judge=StubJudge(HELD),
            reader=reader(),
        )
        assert [row.name for row in report.fired] == ["house style"]

    def test_a_declared_row_reaches_the_writer_as_a_rule(self, tmp_path: Path) -> None:
        notes = PipelineNotes(tmp_path)
        collector = FormatCheckCollector(format_checks_path(notes))
        collector.declared.checks.append(
            BoldedSummaries(
                name="house bolding", rule="Bold each claim.", share_floor=1.0
            )
        )
        collector.save()
        declared = load_declared_checks(format_checks_path(notes))
        guidance = get_format_guidance("custom:a house memo", declared)
        assert "house bolding" in guidance
        assert "Bold each claim." in guidance

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError):
            DeclareFormatCheckInput.model_validate(
                {"check": {"kind": "vibes", "name": "x", "rule": "y"}}
            )
