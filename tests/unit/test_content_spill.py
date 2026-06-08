"""Tests for content spill — safety-net for oversized tool output."""

from pathlib import Path

from inkwell.agent.tools.content_spill import (
    SPILL_THRESHOLD,
    should_spill,
    spill,
    spill_instruction,
)


class TestShouldSpill:
    def test_short_content_does_not_spill(self) -> None:
        assert not should_spill("hello world")

    def test_at_threshold_does_not_spill(self) -> None:
        assert not should_spill("x" * SPILL_THRESHOLD)

    def test_over_threshold_spills(self) -> None:
        assert should_spill("x" * (SPILL_THRESHOLD + 1))

    def test_empty_content_does_not_spill(self) -> None:
        assert not should_spill("")


class TestSpill:
    def test_writes_file_and_returns_path(self, tmp_path: Path, monkeypatch: object) -> None:
        import inkwell.agent.tools.content_spill as mod

        monkeypatch.setattr(mod, "SPILL_DIR", tmp_path / "spill")  # type: ignore[attr-defined]
        content = "x" * 30_000
        path = spill("fetch", "https://example.com", content)
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content
        assert path.name.startswith("fetch_")
        assert path.name.endswith(".md")

    def test_idempotent_for_same_label(self, tmp_path: Path, monkeypatch: object) -> None:
        import inkwell.agent.tools.content_spill as mod

        monkeypatch.setattr(mod, "SPILL_DIR", tmp_path / "spill")  # type: ignore[attr-defined]
        path1 = spill("fetch", "same-label", "content v1")
        path2 = spill("fetch", "same-label", "content v2")
        assert path1 == path2
        assert path2.read_text(encoding="utf-8") == "content v2"

    def test_different_labels_produce_different_files(self, tmp_path: Path, monkeypatch: object) -> None:
        import inkwell.agent.tools.content_spill as mod

        monkeypatch.setattr(mod, "SPILL_DIR", tmp_path / "spill")  # type: ignore[attr-defined]
        path1 = spill("fetch", "label-a", "content a")
        path2 = spill("fetch", "label-b", "content b")
        assert path1 != path2

    def test_creates_spill_directory(self, tmp_path: Path, monkeypatch: object) -> None:
        import inkwell.agent.tools.content_spill as mod

        spill_dir = tmp_path / "nested" / "spill"
        monkeypatch.setattr(mod, "SPILL_DIR", spill_dir)  # type: ignore[attr-defined]
        spill("fetch", "test", "content")
        assert spill_dir.exists()


class TestSpillInstruction:
    def test_includes_path_and_word_count(self) -> None:
        result = spill_instruction(Path("notes/spill/fetch_abc123.md"), 5000)
        assert "notes/spill/fetch_abc123.md" in result
        assert "5000 words" in result
        assert "Read" in result
