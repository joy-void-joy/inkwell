"""Tests for the shared cross-writer glossary."""

from pathlib import Path

from inkwell.agent.tools.stage_outputs import (
    define_term_in_glossary,
    load_glossary,
    seed_glossary,
)


class TestGlossary:
    def test_seed_records_conventions(self, tmp_path: Path) -> None:
        path = tmp_path / "glossary.json"
        seed_glossary(path, ["call the protocol 'the handshake'"])
        conventions, terms = load_glossary(path)
        assert conventions == ["call the protocol 'the handshake'"]
        assert terms == []

    def test_first_definition_wins(self, tmp_path: Path) -> None:
        path = tmp_path / "glossary.json"
        seed_glossary(path, [])
        first = define_term_in_glossary(path, "widget", "a small gadget")
        assert first.already_defined is False
        # A sibling proposing a rival meaning gets handed the canonical one.
        second = define_term_in_glossary(path, "Widget", "something different")
        assert second.already_defined is True
        assert second.meaning == "a small gadget"

    def test_seed_preserves_coined_terms(self, tmp_path: Path) -> None:
        path = tmp_path / "glossary.json"
        seed_glossary(path, ["conv a"])
        define_term_in_glossary(path, "widget", "a gadget")
        # Re-seeding (e.g. on restart) refreshes conventions, keeps terms.
        seed_glossary(path, ["conv a", "conv b"])
        conventions, terms = load_glossary(path)
        assert conventions == ["conv a", "conv b"]
        assert [t.term for t in terms] == ["widget"]

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        conventions, terms = load_glossary(tmp_path / "absent.json")
        assert conventions == []
        assert terms == []
