"""A document the author references but never attached must not slip through.

When pasted or uploaded source material is lost before it reaches the pipeline
(a dropped upload, an unsent paste), the only input left is the author's
directions — which talk about "the document" that isn't there. The preprocess
guard catches that: it detects instructions that presuppose absent source
material and halts instead of silently writing from the directions alone. These
tests pin the discriminator (is any real source present?), the halt at the
preprocess boundary, and the log preview that makes the drop visible.
"""

from pathlib import Path

import pytest

from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    PipelineRunner,
    PipelineStopRequested,
    summarize_inputs,
)
from inkwell.agent.tools import preprocess as preprocess_mod
from inkwell.agent.tools.preprocess import ClassifiedSource, PreprocessResult


class TestHasConcreteSource:
    """The discriminator: a real document (URL or file on disk) vs. prose."""

    def test_freeform_only_has_no_concrete_source(self) -> None:
        result = PreprocessResult(
            raw_inputs=["rewrite the document"],
            classified=[ClassifiedSource(value="rewrite the document", role="source")],
        )
        assert result.has_concrete_source is False

    def test_embedded_url_counts(self) -> None:
        result = PreprocessResult(
            raw_inputs=["see https://example.com"],
            classified=[
                ClassifiedSource(
                    value="see https://example.com",
                    role="source",
                    discovered_urls=["https://example.com"],
                )
            ],
        )
        assert result.has_concrete_source is True

    def test_existing_file_counts(self, tmp_path: Path) -> None:
        doc = tmp_path / "doc.md"
        doc.write_text("hi", encoding="utf-8")
        result = PreprocessResult(
            raw_inputs=[str(doc)],
            classified=[ClassifiedSource(value=str(doc), role="source")],
        )
        assert result.has_concrete_source is True

    def test_only_source_role_counts(self) -> None:
        # A style-reference URL is not primary source material, so an absent
        # primary source is still absent even though a URL is present.
        result = PreprocessResult(
            raw_inputs=["rewrite the doc", "https://example.com"],
            classified=[
                ClassifiedSource(value="rewrite the doc", role="source"),
                ClassifiedSource(
                    value="https://example.com",
                    role="style_reference",
                    discovered_urls=["https://example.com"],
                ),
            ],
        )
        assert result.has_concrete_source is False


class TestSummarizeInputs:
    def test_labels_text_url_and_file(self, tmp_path: Path) -> None:
        doc = tmp_path / "paper.md"
        doc.write_text("x", encoding="utf-8")
        summary = summarize_inputs(
            [str(doc), "https://example.com/a", "just some prose"]
        )
        assert "[file] paper.md" in summary
        assert "[url] https://example.com/a" in summary
        assert "[text 15ch]" in summary

    def test_empty_inputs(self) -> None:
        assert summarize_inputs([]) == "(none)"


class TestWarnMissingSource:
    async def test_halts_with_actionable_message(self, tmp_path: Path) -> None:
        runner = PipelineRunner(sources=["x"], notes=PipelineNotes(tmp_path / "n"))
        with pytest.raises(PipelineStopRequested) as exc:
            await runner.warn_missing_source("the document is missing")
        assert exc.value.stage == "preprocess"
        assert "Re-create" in exc.value.message


class TestHandlePauseMessageOverride:
    async def test_custom_message_becomes_summary(self, tmp_path: Path) -> None:
        runner = PipelineRunner(sources=["x"], notes=PipelineNotes(tmp_path / "n"))
        runner.doc_id = "d"
        runner.doc_url = "u"

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        runner.update_overview = noop  # type: ignore[method-assign]  # claude: ignore
        output = await runner.handle_pause("preprocess", "custom reason here")
        assert output.summary == "custom reason here"
        assert output.paused_after == "preprocess"


class TestStagePreprocessGuard:
    """The stage itself halts when a referenced source is absent, and proceeds
    when a real document is present even if the model flags a reference."""

    async def test_pauses_when_referenced_source_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = PipelineRunner(
            sources=["rewrite the document"], notes=PipelineNotes(tmp_path / "n")
        )

        async def fake_preprocess(sources: list[str]) -> PreprocessResult:
            return PreprocessResult(
                raw_inputs=sources,
                instructions="rewrite the document",
                references_absent_source=True,
                absent_source_note="refers to 'the document'",
                classified=[ClassifiedSource(value=sources[0], role="source")],
            )

        monkeypatch.setattr(preprocess_mod, "preprocess_sources", fake_preprocess)
        with pytest.raises(PipelineStopRequested) as exc:
            await runner.stage_preprocess()
        assert exc.value.stage == "preprocess"

    async def test_proceeds_when_concrete_source_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc = tmp_path / "doc.md"
        doc.write_text("the real document", encoding="utf-8")
        runner = PipelineRunner(sources=[str(doc)], notes=PipelineNotes(tmp_path / "n"))

        async def fake_preprocess(sources: list[str]) -> PreprocessResult:
            return PreprocessResult(
                raw_inputs=sources,
                instructions="rewrite the document",
                references_absent_source=True,
                classified=[ClassifiedSource(value=sources[0], role="source")],
            )

        monkeypatch.setattr(preprocess_mod, "preprocess_sources", fake_preprocess)
        await runner.stage_preprocess()  # concrete source present → no pause
