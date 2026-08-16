"""Tests for the source-document registry and its page-count metadata."""

import shutil
from pathlib import Path

import pytest

from inkwell.agent.tools.source_consult import (
    build_source_registry,
    load_source_registry,
    pdf_page_count,
    registry_path_for,
)

needs_poppler = pytest.mark.skipif(
    shutil.which("pdfinfo") is None,
    reason="pdfinfo (poppler-utils) is not installed",
)


def make_pdf(path: Path, pages: int) -> None:
    """Write a minimal valid PDF with the given number of empty pages.

    Hand-built rather than produced by a library: nothing in this project
    writes PDFs, so a fixture that needed one would be a dependency carried
    for a test alone.
    """
    kids = " ".join(f"{index + 3} 0 R" for index in range(pages))
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        f"<</Type/Pages/Kids[{kids}]/Count {pages}>>".encode(),
        *[b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"] * pages,
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj".encode() + payload + b"endobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode()
    body += b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (
        f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(body))


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    target = tmp_path / "artifacts"
    target.mkdir()
    return target


class TestPageCount:
    @needs_poppler
    def test_counts_the_pages_poppler_reports(self, tmp_path: Path) -> None:
        pdf = tmp_path / "thesis.pdf"
        make_pdf(pdf, 3)

        assert pdf_page_count(pdf) == 3

    @needs_poppler
    def test_registration_records_the_count(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        pdf = tmp_path / "thesis.pdf"
        make_pdf(pdf, 2)

        docs = build_source_registry([str(pdf)], artifacts_dir)

        assert len(docs) == 1
        assert docs[0].kind == "pdf"
        assert docs[0].page_count == 2

    def test_an_unreadable_pdf_registers_with_no_page_windows(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        """A count that cannot be read is zero, never a guess.

        Zero reads downstream as "page windows unknown, take it whole",
        which is the honest answer — a document still registers and stays
        readable rather than being dropped for want of metadata.
        """
        pdf = tmp_path / "broken.pdf"
        pdf.write_bytes(b"not a pdf at all")

        docs = build_source_registry([str(pdf)], artifacts_dir)

        assert len(docs) == 1
        assert docs[0].kind == "pdf"
        assert docs[0].page_count == 0


class TestSourceRegistry:
    def test_missing_paths_are_skipped(self, artifacts_dir: Path) -> None:
        docs = build_source_registry(["/nonexistent/file.pdf"], artifacts_dir)
        assert docs == []
        assert load_source_registry(registry_path_for(artifacts_dir)) == []

    def test_text_file_registration(self, tmp_path: Path, artifacts_dir: Path) -> None:
        note = tmp_path / "essay.md"
        note.write_text("plain text source", encoding="utf-8")
        docs = build_source_registry([str(note)], artifacts_dir)
        assert docs[0].kind == "text"
        assert docs[0].page_count == 0

    def test_the_registry_round_trips(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        note = tmp_path / "thesis.md"
        note.write_text("proof content", encoding="utf-8")
        build_source_registry([str(note)], artifacts_dir)

        reloaded = load_source_registry(registry_path_for(artifacts_dir))

        assert [d.label for d in reloaded] == ["thesis"]

    def test_original_is_copied_into_the_sandbox_reachable_tree(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        note = tmp_path / "thesis.md"
        note.write_text("proof content", encoding="utf-8")
        docs = build_source_registry([str(note)], artifacts_dir)
        registered = Path(docs[0].path)
        assert registered.parent == artifacts_dir / "sources"
        assert registered.read_text(encoding="utf-8") == "proof content"
