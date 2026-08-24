"""A document the author references but never attached must not slip through.

When pasted or uploaded source material is lost before it reaches the pipeline
(a dropped upload, an unsent paste), the only input left is the author's
directions — which talk about "the document" that isn't there. The extract
guard catches that: the orchestrator flags instructions that presuppose absent
source material, and the extract stage halts instead of silently writing from
the directions alone. These tests pin the discriminator (is any real source
present?), the halt at the extract boundary, and the log preview that makes the
drop visible.
"""

from pathlib import Path

import pytest

import inkwell.agent.pipeline as pipeline_module
from inkwell.agent.extract_agent import ExtractedSource, ExtractionManifest
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import (
    PipelineRunner,
    PipelineStopRequested,
    summarize_inputs,
)


class TestHasConcreteSource:
    """The discriminator: a real fetched document (URL or file) vs. inline prose."""

    def test_inline_only_has_no_concrete_source(self) -> None:
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="rewrite the document", role="source", origin="inline"
                )
            ]
        )
        assert manifest.has_concrete_source is False

    def test_url_source_counts(self) -> None:
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="https://example.com", role="source", origin="url"
                )
            ]
        )
        assert manifest.has_concrete_source is True

    def test_file_source_counts(self) -> None:
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(raw_input="/tmp/doc.md", role="source", origin="file")
            ]
        )
        assert manifest.has_concrete_source is True

    def test_only_source_role_counts(self) -> None:
        # A style-reference URL is not primary source material, so an absent
        # primary source is still absent even though a URL is present.
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="rewrite the doc", role="source", origin="inline"
                ),
                ExtractedSource(
                    raw_input="https://example.com",
                    role="style_reference",
                    origin="url",
                ),
            ]
        )
        assert manifest.has_concrete_source is False


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
        assert exc.value.stage == "extract"
        assert "Re-create" in exc.value.message


class TestHandlePauseMessageOverride:
    async def test_custom_message_becomes_summary(self, tmp_path: Path) -> None:
        runner = PipelineRunner(sources=["x"], notes=PipelineNotes(tmp_path / "n"))
        runner.doc_id = "d"
        runner.doc_url = "u"

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        runner.update_overview = noop  # type: ignore[method-assign]  # claude: ignore
        output = await runner.handle_pause("extract", "custom reason here")
        assert output.summary == "custom reason here"
        assert output.paused_after == "extract"


class TestStageExtractGuard:
    """The extract stage halts when the orchestrator flags a referenced source as
    absent and no real document is among the extracted sources."""

    async def test_pauses_when_referenced_source_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = PipelineRunner(
            sources=["rewrite the document"], notes=PipelineNotes(tmp_path / "n")
        )

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        runner.announce_stage = noop  # type: ignore[method-assign]  # claude: ignore
        runner.update_overview = noop  # type: ignore[method-assign]  # claude: ignore

        async def fake_extract(*_args: object, **_kwargs: object) -> ExtractionManifest:
            return ExtractionManifest(
                instructions="rewrite the document",
                references_absent_source=True,
                absent_source_note="refers to 'the document'",
                sources=[
                    ExtractedSource(
                        raw_input="rewrite the document",
                        role="source",
                        origin="inline",
                    )
                ],
            )

        monkeypatch.setattr(pipeline_module, "run_extraction_agent", fake_extract)
        with pytest.raises(PipelineStopRequested) as exc:
            await runner.stage_extract()
        assert exc.value.stage == "extract"

    def test_concrete_source_keeps_the_stage_running(self, tmp_path: Path) -> None:
        # A flagged reference plus a real document means the document arrived;
        # the guard's gate (flagged AND nothing concrete) must be false.
        doc = tmp_path / "doc.md"
        doc.write_text("the real document", encoding="utf-8")
        manifest = ExtractionManifest(
            references_absent_source=True,
            sources=[ExtractedSource(raw_input=str(doc), role="source", origin="file")],
        )
        assert not (
            manifest.references_absent_source and not manifest.has_concrete_source
        )


class TestStageExtractRecovery:
    """Only source-shaped inputs enter deterministic fallback extraction."""

    async def test_multiline_instructions_are_not_fetched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "part.md"
        source.write_text("## Part\n", encoding="utf-8")
        instructions = "Write this part.\n\nKeep its heading."
        runner = PipelineRunner(
            sources=[str(source), instructions],
            notes=PipelineNotes(tmp_path / "notes"),
        )

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(runner, "announce_stage", noop)
        monkeypatch.setattr(runner, "update_overview", noop)

        async def fake_extract(*_args: object, **_kwargs: object) -> ExtractionManifest:
            return ExtractionManifest(
                instructions=instructions,
                sources=[
                    ExtractedSource(
                        raw_input=str(source),
                        role="source",
                        origin="file",
                        local_path=str(source),
                    )
                ],
            )

        attempted: list[str] = []

        async def record_fetch(value: str, _doc_id: str | None) -> str:
            attempted.append(value)
            return ""

        class SnapshotSaved(Exception):
            pass

        async def stop_after_snapshot() -> None:
            raise SnapshotSaved

        monkeypatch.setattr(runner, "save_snapshot", stop_after_snapshot)
        monkeypatch.setattr(pipeline_module, "run_extraction_agent", fake_extract)
        monkeypatch.setattr(pipeline_module, "extract_single_source", record_fetch)

        with pytest.raises(SnapshotSaved):
            await runner.stage_extract()
        assert attempted == []
        assert runner.snapshot.author_instructions == instructions
        assert runner.snapshot.conversation.endswith("## Part\n")
