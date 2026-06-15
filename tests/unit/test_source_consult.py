"""Tests for source-document registry, text layer, and navigation search."""

from pathlib import Path

import pymupdf
import pytest

from inkwell.agent.tools.source_consult import (
    FindInSourceInput,
    build_source_registry,
    load_source_registry,
    make_source_consult_tools,
    registry_path_for,
)


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    target = tmp_path / "artifacts"
    target.mkdir()
    return target


class TestSourceRegistry:
    def test_pdf_registration_builds_per_page_text_layer(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        pdf = tmp_path / "thesis.pdf"
        make_pdf(
            pdf,
            [
                "Integers are represented least significant digit first.",
                "Definition 2.1: a vocabulary is a set of symbols.",
            ],
        )
        docs = build_source_registry([str(pdf)], artifacts_dir)

        assert len(docs) == 1
        assert docs[0].kind == "pdf"
        assert docs[0].page_count == 2
        text_dir = Path(docs[0].text_dir)
        assert (text_dir / "0001.txt").exists()
        assert "least significant" in (text_dir / "0001.txt").read_text()

        reloaded = load_source_registry(registry_path_for(artifacts_dir))
        assert [d.label for d in reloaded] == ["thesis"]

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

    def test_original_is_copied_into_the_sandbox_reachable_tree(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        note = tmp_path / "thesis.md"
        note.write_text("proof content", encoding="utf-8")
        docs = build_source_registry([str(note)], artifacts_dir)
        registered = Path(docs[0].path)
        assert registered.parent == artifacts_dir / "sources"
        assert registered.read_text(encoding="utf-8") == "proof content"


class TestFindInSource:
    async def test_finds_pattern_with_page_number(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        pdf = tmp_path / "paper.pdf"
        make_pdf(
            pdf,
            [
                "Introduction with nothing relevant.",
                "The encoding reads least significant digit first.",
            ],
        )
        build_source_registry([str(pdf)], artifacts_dir)
        tools = make_source_consult_tools(lambda: registry_path_for(artifacts_dir))
        find = next(t for t in tools if t.sdk_tool.name == "find_in_source")

        result = await find.sdk_tool.handler(
            FindInSourceInput(pattern="least significant").model_dump()
        )

        assert result.get("is_error") is not True
        text = str(result["content"][0]["text"])
        assert '"page": 2' in text.replace("page=", '"page": ') or '"page":2' in (
            text.replace(" ", "")
        )

    async def test_empty_registry_is_actionable_error(
        self, artifacts_dir: Path
    ) -> None:
        tools = make_source_consult_tools(lambda: registry_path_for(artifacts_dir))
        find = next(t for t in tools if t.sdk_tool.name == "find_in_source")

        result = await find.sdk_tool.handler(
            FindInSourceInput(pattern="x").model_dump()
        )

        assert result.get("is_error") is True

    async def test_invalid_regex_is_actionable_error(
        self, tmp_path: Path, artifacts_dir: Path
    ) -> None:
        pdf = tmp_path / "paper.pdf"
        make_pdf(pdf, ["content"])
        build_source_registry([str(pdf)], artifacts_dir)
        tools = make_source_consult_tools(lambda: registry_path_for(artifacts_dir))
        find = next(t for t in tools if t.sdk_tool.name == "find_in_source")

        result = await find.sdk_tool.handler(
            FindInSourceInput(pattern="[bad").model_dump()
        )

        assert result.get("is_error") is True
