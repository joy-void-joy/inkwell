# claude: ignore
"""Tests for what a format declares, and where those declarations reach."""

from pathlib import Path

import pytest

from inkwell.agent.content import ContentManifest
from inkwell.agent.format_checks import (
    BoldedSummaries,
    BoldEmphasis,
    JudgedRow,
    run_format_checks,
)
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import add_format_check_report, format_checks_path
from inkwell.agent.segmenter import reader
from inkwell.agent.stages import (
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
        """The voice document rations semicolons and parentheses too."""
        assert "Semicolons are rationed" in self.spec.guidance
        assert "Parentheses are rationed" in self.spec.guidance
        assert "No markdown left showing" in self.spec.guidance

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
        from inkwell.environment.cli.__main__ import FORMAT_HELP

        assert "textbook" in FORMAT_HELP
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

    def test_unknown_format_declares_nothing(self) -> None:
        assert format_checks_for("auto") == []
        assert get_format_guidance("auto") == ""


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
        mechanical = [c for c in format_checks_for("textbook") if c.kind != "judged"]
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
        mechanical = [c for c in format_checks_for("textbook") if c.kind != "judged"]
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
