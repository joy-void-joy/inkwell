"""A source file's encoding must not decide whether the run survives.

The extract stage assembles the conversation blob from whatever the extraction
agent wrote to disk. A PDF is on that list: the fetch tool downloads the bytes
and records where they landed, because a PDF is read a page-window at a time by
a reader holding ``Read`` rather than decoded into the blob. Reading those bytes
as UTF-8 took a whole seven-stage run down at stage one, so these tests pin the
three-way sort — text to inline, documents to register, gaps to re-extract — and
that no unreadable file raises.
"""

from pathlib import Path

from inkwell.agent.extract_agent import (
    ExtractedSource,
    ExtractionManifest,
    assemble_sources,
    read_source_content,
)
from inkwell.pdf import reads_by_page

PDF_HEADER = b"%PDF-1.7\r\n%\xb5\xb5\xb5\xb5\r\n1 0 obj\r\n"


def pdf_at(directory: Path, name: str = "paper.pdf") -> Path:
    """A file whose bytes are a real PDF preamble — undecodable as UTF-8."""
    path = directory / name
    path.write_bytes(PDF_HEADER)
    return path


class TestReadsByPage:
    """The one question every surface sorting source files asks."""

    def test_a_pdf_reads_by_page(self) -> None:
        assert reads_by_page(Path("/tmp/paper.pdf")) is True

    def test_case_is_not_the_question(self) -> None:
        assert reads_by_page(Path("/tmp/PAPER.PDF")) is True

    def test_markdown_does_not(self) -> None:
        assert reads_by_page(Path("/tmp/draft.md")) is False

    def test_a_pdf_in_the_stem_is_not_a_suffix(self) -> None:
        assert reads_by_page(Path("/tmp/about-pdf-files.md")) is False


class TestReadSourceContent:
    """What one manifest entry yields, and what it refuses to raise."""

    def test_utf8_file_reads_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "draft.md"
        path.write_text("## Cyber Risk\n", encoding="utf-8")
        entry = ExtractedSource(
            raw_input="/draft.md", role="source", origin="file", local_path=str(path)
        )
        assert read_source_content(entry) == "## Cyber Risk\n"

    def test_undecodable_bytes_yield_empty_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        entry = ExtractedSource(
            raw_input="https://example.com/paper.pdf",
            role="context",
            origin="url",
            local_path=str(pdf_at(tmp_path)),
        )
        assert read_source_content(entry) == ""

    def test_unreadable_directory_path_yields_empty(self, tmp_path: Path) -> None:
        entry = ExtractedSource(
            raw_input="/somewhere",
            role="source",
            origin="file",
            local_path=str(tmp_path),
        )
        assert read_source_content(entry) == ""

    def test_inline_prose_falls_back_to_raw_input(self) -> None:
        entry = ExtractedSource(
            raw_input="write about cyber risk", role="source", origin="inline"
        )
        assert read_source_content(entry) == "write about cyber risk"


class TestAssembleSources:
    """The three-way sort every entry lands in exactly one of."""

    def test_pdf_registers_as_a_document_not_a_block(self, tmp_path: Path) -> None:
        path = pdf_at(tmp_path)
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="https://example.com/paper.pdf",
                    role="context",
                    origin="url",
                    local_path=str(path),
                )
            ]
        )
        assembled = assemble_sources(manifest)
        assert assembled.documents == [str(path)]
        assert assembled.blocks == []

    def test_a_pdf_is_never_unrecovered(self, tmp_path: Path) -> None:
        """Unrecovered sends the caller back to the network; the bytes are here."""
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="https://example.com/paper.pdf",
                    role="source",
                    origin="url",
                    local_path=str(pdf_at(tmp_path)),
                )
            ]
        )
        assert assemble_sources(manifest).unrecovered == []

    def test_text_source_becomes_a_labelled_block(self, tmp_path: Path) -> None:
        path = tmp_path / "draft.md"
        path.write_text("## Cyber Risk\n", encoding="utf-8")
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="/draft.md",
                    role="source",
                    origin="file",
                    local_path=str(path),
                )
            ]
        )
        assembled = assemble_sources(manifest)
        assert assembled.blocks == ["--- Source: /draft.md ---\n\n## Cyber Risk\n"]
        assert assembled.documents == []

    def test_context_role_labels_the_block_a_reference(self, tmp_path: Path) -> None:
        path = tmp_path / "note.md"
        path.write_text("background\n", encoding="utf-8")
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="https://example.com/note",
                    role="context",
                    origin="url",
                    local_path=str(path),
                )
            ]
        )
        blocks = assemble_sources(manifest).blocks
        assert blocks == ["--- Reference: https://example.com/note ---\n\nbackground\n"]

    def test_missing_content_is_unrecovered(self) -> None:
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="https://example.com/dead", role="source", origin="url"
                )
            ]
        )
        assembled = assemble_sources(manifest)
        assert assembled.unrecovered == ["https://example.com/dead"]
        assert assembled.blocks == []

    def test_style_references_reach_neither_bucket(self, tmp_path: Path) -> None:
        path = tmp_path / "voice.md"
        path.write_text("my voice\n", encoding="utf-8")
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="/voice.md",
                    role="style_reference",
                    origin="file",
                    local_path=str(path),
                )
            ]
        )
        assembled = assemble_sources(manifest)
        assert assembled.blocks == []
        assert assembled.documents == []
        assert assembled.unrecovered == []

    def test_a_mixed_manifest_sorts_every_entry_once(self, tmp_path: Path) -> None:
        draft = tmp_path / "draft.md"
        draft.write_text("## Cyber Risk\n", encoding="utf-8")
        pdf = pdf_at(tmp_path)
        manifest = ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input="/draft.md",
                    role="source",
                    origin="file",
                    local_path=str(draft),
                ),
                ExtractedSource(
                    raw_input="https://example.com/paper.pdf",
                    role="context",
                    origin="url",
                    local_path=str(pdf),
                ),
                ExtractedSource(
                    raw_input="https://example.com/dead", role="context", origin="url"
                ),
            ]
        )
        assembled = assemble_sources(manifest)
        assert len(assembled.blocks) == 1
        assert assembled.documents == [str(pdf)]
        assert assembled.unrecovered == ["https://example.com/dead"]
